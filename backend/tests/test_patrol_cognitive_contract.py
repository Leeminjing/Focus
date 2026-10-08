"""本文件验证 Patrol 认知步骤的单一有效 Mission、语义引用、扁平判断折叠、非法形状与有界重试。

输入为脚本化模型 JSON 与协调器桩；输出为语义引用、折叠结果、违例携带的模型原始输出及重试计数断言。具体工作流为在
无网络条件下调用结构化解析边界、决策合同与调度重试。示例：`pytest test_patrol_cognitive_contract.py`。
冻结阶段演练通过既有模型入口核验合法串行、required 的独立模块子集及单结果采用，真实模型结果另行记录。
"""

import asyncio
import json
from types import SimpleNamespace

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
from backend.app.desktop.agent_loop.patrol_runtime import CuratorCoordinationStage
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.tests.config_helpers import app_config_for

_DECISION = {"rationale": "继续推进", "evidence": [], "mission_references": [{"role": "outcome", "reference_id": "outcome"}], "actions": [{"action": "continue_context", "context_id": "context-1", "context_revision_id": "revision-1", "message": "继续既有工作"}]}
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
    assert step.decision.actions[0].action == "continue_context"


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


def test_patrol_model_receives_only_one_effective_mission(monkeypatch):
    model = _scripted_model(json.dumps(_DECISION, ensure_ascii=False))
    decision_model = StructuredPatrolDecisionModel(_prompt_json_config())
    monkeypatch.setattr(module, "create_chat_model", lambda **kwargs: model)
    observation = _required_expansion_observation().model_copy(update={
        "grant": {"grant_id": "grant-1", "holder_id": "patrol-1"},
        "expansion_assessment": None,
    })

    _run(decision_model(observation))

    payload = json.loads(str(model.received[0][1].content).removeprefix("<loop_observation>").removesuffix("</loop_observation>"))
    assert payload["effective_mission"] == {"outcome": "finish"}
    assert "goal" not in payload
    assert "mission" not in payload


def test_curator_input_projects_legacy_goal_to_one_mission():
    class CatalogService:
        async def build(self, observation):
            return SimpleNamespace(catalog=SimpleNamespace(model_dump=lambda **kwargs: {"catalog_id": "catalog"}), indexes=(), stage_record=None)

    stage = CuratorCoordinationStage(object(), index_service=CatalogService())
    observation = _required_expansion_observation().model_copy(update={
        "mission": None,
        "goal": {"goal": "旧目标", "task_contract": "边界原文", "acceptance_criteria": [{"criterion_id": "tests", "text": "测试通过"}]},
    })

    payload = _run(stage._derivation_input(observation))

    assert payload["mission"]["outcome"] == "旧目标"
    assert payload["mission"]["boundaries"]["legacy_text"] == "边界原文"
    assert payload["mission"]["completion_checks"][0]["check_id"] == "tests"
    assert "goal" not in payload


@pytest.mark.parametrize("stage", ["foundation", "modules", "shared_interface", "adoption"])
def test_stage_actions_use_existing_frozen_contract_and_required_subset(monkeypatch, stage):
    from backend.tests.worktree_stage_drill_support import stage_observations, accepts_stage_decision

    if stage == "modules":
        actions = [{"action": "spawn_context", "opportunity_id": identity * 64} for identity in ("c", "d")]
    elif stage == "adoption":
        actions = [{"action": "adopt_workspace_result", "source_slot_id": "slot-a", "source_revision": 2, "rationale": "Verified result"}]
    else:
        actions = [{"action": "continue_context", "context_id": "primary", "context_revision_id": "revision-primary", "message": "Validate foundation/interface first"}]
    model = _scripted_model(json.dumps({**_DECISION, "actions": actions}))
    monkeypatch.setattr(module, "create_chat_model", lambda **kwargs: model)
    intent = _run(StructuredPatrolDecisionModel(_prompt_json_config())(stage_observations()[stage]))
    assert accepts_stage_decision(stage, intent.actions)


def test_invalid_shape_raises_retryable_violation_with_raw_output():
    payload = json.dumps({"decision": {"rationale": "缺少 actions"}})
    model = _scripted_model(payload)
    decision_model = StructuredPatrolDecisionModel(app_config_for("patrol-test", None))

    with pytest.raises(PatrolContractViolation) as error:
        _run(decision_model._invoke(model, [], "prompt_json", PatrolCognitiveStep))

    assert error.value.raw_output == payload


def test_semantic_contract_violation_carries_raw_output(monkeypatch):
    payload = json.dumps(
        {
            "rationale": "选择一个不在 assessment 内的 opportunity",
            "mission_references": [{"role": "outcome", "reference_id": "outcome"}],
            "actions": [{"action": "spawn_context", "opportunity_id": "b" * 64}],
        }
    )
    model = _scripted_model(payload)
    decision_model = StructuredPatrolDecisionModel(_prompt_json_config())
    monkeypatch.setattr(module, "create_chat_model", lambda **kwargs: model)

    with pytest.raises(PatrolContractViolation) as error:
        _run(decision_model(_required_expansion_observation()))

    assert error.value.raw_output == payload
    assert "assessment 之外" in str(error.value)


def test_contract_violation_is_retried_then_succeeds(monkeypatch):
    orchestrator, calls = _orchestrator(monkeypatch, [PatrolContractViolation("形状非法"), None])

    intent = _run(orchestrator._decide_patrol(_claim(), None, "patrol:1", object()))

    assert intent == "intent"
    assert calls["decide"] == 2
    assert calls["retry"] == 1
    assert calls["fail"] == 0


def test_retry_feedback_repairs_malformed_proposal_on_frozen_observation(monkeypatch):
    from backend.tests.config_helpers import ToolCapableFakeChatModel

    model = ToolCapableFakeChatModel(scripted=[
        AIMessage(content=json.dumps({"decision": {"rationale": "缺少 actions"}})),
        AIMessage(content=json.dumps({"decision": _DECISION}, ensure_ascii=False)),
    ])
    monkeypatch.setattr(module, "create_chat_model", lambda **kwargs: model)
    observation = _required_expansion_observation().model_copy(update={
        "grant": {"grant_id": "grant-1", "holder_id": "patrol:1"},
        "expansion_assessment": None,
    })

    class DirectPatrol:
        def __init__(self, sessions, decision_model):
            self._decision_model = decision_model

        async def decide(self, frozen_observation, holder_id):
            assert frozen_observation is observation
            return await self._decision_model(frozen_observation)

    monkeypatch.setattr(module, "PortfolioPatrol", DirectPatrol)
    orchestrator = LoopRoundOrchestrator(sessions=None, app_config=_prompt_json_config(), kernel=None, checkpointer=None)

    async def no_op(*args):
        return None

    orchestrator._record_usage = no_op
    orchestrator._record_retry = no_op
    orchestrator._fail = no_op

    intent = _run(orchestrator._decide_patrol(_claim(), None, "patrol:1", observation))

    assert intent.actions[0].action == "continue_context"
    assert len(model.received) == 2
    assert model.received[0][1].content == model.received[1][1].content
    feedback = " ".join(str(message.content) for message in model.received[1][2:])
    assert "actions" in feedback and "missing" in feedback
    assert "缺少 actions" not in feedback


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


def _prompt_json_config():
    config = app_config_for("patrol-test", None)
    entry = config.models[0].model_copy(update={"curation_output_method": "prompt_json"})
    return config.model_copy(update={"models": [entry]})


def _required_expansion_observation() -> LoopObservationEnvelope:
    return LoopObservationEnvelope(
        loop_id="loop-1",
        loop_revision=1,
        round_id="round-1",
        goal_revision=1,
        authority_revision=1,
        observed_frontier_hash="a" * 64,
        mission={"outcome": "finish"},
        grant={},
        portfolio_frontier=(),
        workspace={"revision": 1},
        budget={},
        expansion_assessment={
            "loop_id": "loop-1",
            "round_id": "round-1",
            "frontier_hash": "a" * 64,
            "policy_version": "context-expansion-v1",
            "level": "required",
            "opportunities": ({"opportunity_id": "a" * 64},),
            "blockers": (),
        },
    )


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
