"""本文件验证 Patrol 认知步骤的 Mission 引用、扁平判断折叠、非法形状拒绝与有界重试。

输入为脚本化模型 JSON 与协调器桩；输出为语义引用、折叠结果及重试计数断言。具体工作流为在
无网络条件下调用结构化解析边界与调度重试。示例：`pytest test_patrol_cognitive_contract.py`。
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from backend.app.desktop.agent_loop import round_orchestration as module
from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim
from backend.app.desktop.agent_loop.patrol import PatrolContractViolation
from backend.app.desktop.agent_loop.round_orchestration import (
    LoopRoundOrchestrator,
    PatrolCognitiveStep,
    StructuredPatrolDecisionModel,
)
from backend.tests.config_helpers import app_config_for

_DECISION = {"rationale": "继续推进", "evidence": [], "mission_references": [{"role": "outcome", "reference_id": "outcome"}], "actions": [{"action": "wait_for_user", "reason": "等用户"}]}
_FLAT = {"rationale": _DECISION["rationale"], "evidence": [], "mission_references": _DECISION["mission_references"], "actions": _DECISION["actions"]}


def _run(coro):
    return asyncio.run(coro)


def test_flat_proposal_folds_into_decision():
    step = PatrolCognitiveStep.model_validate(_FLAT)
    assert step.decision is not None
    assert step.decision.rationale == "继续推进"
    assert step.reads == ()


def test_nested_proposal_keeps_shape():
    step = PatrolCognitiveStep.model_validate({"decision": _DECISION})
    assert step.decision is not None
    assert step.decision.actions[0].action == "wait_for_user"


def test_unknown_top_level_key_is_still_rejected():
    with pytest.raises(ValidationError):
        PatrolCognitiveStep.model_validate({**_FLAT, "unexpected": 1})


def test_reads_with_flat_rationale_is_not_silently_folded():
    with pytest.raises(ValidationError):
        PatrolCognitiveStep.model_validate({
            "reads": [{"context_id": "c1", "revision_id": "r1"}],
            "rationale": "顺便说一句",
        })


def test_flat_answer_is_accepted_at_the_model_call_seam():
    model = _scripted_model(json.dumps(_FLAT))
    decision_model = StructuredPatrolDecisionModel(app_config_for("patrol-test", None))

    step = _run(decision_model._invoke(model, [], "prompt_json", PatrolCognitiveStep))

    assert step.decision is not None
    assert step.decision.rationale == "继续推进"


def test_invalid_shape_raises_retryable_violation_with_raw_output():
    payload = json.dumps({"decision": {"rationale": "缺少 actions"}})
    model = _scripted_model(payload)
    decision_model = StructuredPatrolDecisionModel(app_config_for("patrol-test", None))

    with pytest.raises(PatrolContractViolation) as error:
        _run(decision_model._invoke(model, [], "prompt_json", PatrolCognitiveStep))

    assert error.value.raw_output == payload


def test_contract_violation_is_retried_then_succeeds(monkeypatch):
    orchestrator, calls = _orchestrator(monkeypatch, [PatrolContractViolation("形状非法"), None])

    intent = _run(orchestrator._decide_patrol(_claim(), None, "patrol:1", object()))

    assert intent == "intent"
    assert calls["decide"] == 2
    assert calls["retry"] == 1
    assert calls["fail"] == 0


def test_contract_violation_exhausts_attempts(monkeypatch):
    orchestrator, calls = _orchestrator(monkeypatch, [PatrolContractViolation("形状非法")] * 3)

    with pytest.raises(PatrolContractViolation):
        _run(orchestrator._decide_patrol(_claim(), None, "patrol:1", object()))

    assert calls["decide"] == 3
    assert calls["retry"] == 2
    assert calls["fail"] == 1


def test_non_contract_failure_is_not_retried(monkeypatch):
    orchestrator, calls = _orchestrator(monkeypatch, [RuntimeError("模型不可用")])

    with pytest.raises(RuntimeError):
        _run(orchestrator._decide_patrol(_claim(), None, "patrol:1", object()))

    assert calls["decide"] == 1
    assert calls["retry"] == 0
    assert calls["fail"] == 1


def _scripted_model(content: str):
    from backend.tests.config_helpers import ToolCapableFakeChatModel

    return ToolCapableFakeChatModel(scripted=[AIMessage(content=content)])


def _claim() -> CoordinatorClaim:
    return CoordinatorClaim(lease_id="l1", loop_id="loop-1", round_id="round-1", fencing_token="f1")


def _orchestrator(monkeypatch, outcomes: list[Exception | None]):
    """构造只依赖注入桩的编排器：PortfolioPatrol 由 outcomes 驱动，账本与收敛路径记数。"""
    calls = {"decide": 0, "retry": 0, "fail": 0}

    class FakePatrol:
        def __init__(self, sessions, model) -> None:
            self._model = model

        async def decide(self, observation, holder_id):
            outcome = outcomes[min(calls["decide"], len(outcomes) - 1)]
            calls["decide"] += 1
            if outcome is not None:
                raise outcome
            return "intent"

    monkeypatch.setattr(module, "PortfolioPatrol", FakePatrol)
    orchestrator = LoopRoundOrchestrator(sessions=None, app_config=app_config_for("patrol-test", None), kernel=None, checkpointer=None)

    async def record_usage(loop_id, usage) -> None:
        return None

    async def record_retry(loop_id) -> None:
        calls["retry"] += 1

    async def fail(claim, exc) -> None:
        calls["fail"] += 1

    orchestrator._record_usage = record_usage
    orchestrator._record_retry = record_retry
    orchestrator._fail = fail
    return orchestrator, calls
