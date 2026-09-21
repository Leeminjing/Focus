r"""本文件验证 Agent Loop 的纯合同、Mission 引用、用户介入、Patrol 选择性读取和预算边界。

输入为 delegated directive、Patrol cognitive step、workspace adoption action 与 budget usage；输出为模型侧
纯 HumanMessage、真实 OpenAI-compatible 请求、按角色且取值封闭的 Mission 引用、reads/decision 互斥校验、闭合 action 解析和硬预算裁决。
具体工作流为构造严格 schema，并在无网络的 ChatOpenAI invoke 边界截获 provider payload。示例：`pytest test_agent_loop_contracts.py`。
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_openai import ChatOpenAI
from pydantic import ValidationError
import pytest

from backend.app.desktop.agent_loop.budgets import LoopBudgetGuard, configured_provider_count
from backend.app.desktop.agent_loop.models import LoopDirective
from backend.app.desktop.agent_loop.patrol_contract import PatrolDecisionContract
from backend.app.desktop.agent_loop.provenance import DelegatedDirectiveFactory
from backend.app.desktop.agent_loop.round_orchestration import (
    PatrolCognitiveStep,
    PatrolDecisionProposal,
    PatrolReadRequest,
)
from backend.app.desktop.agent_loop.schemas import LoopBudgetContract, LoopInterventionRequest, PATROL_ACTION_ADAPTER
from backend.app.desktop.agent_loop.fact_projection import LoopFactProjectionService


def test_delegated_model_message_contains_no_provenance_marker() -> None:
    directive = LoopDirective(
        directive_id="directive-1",
        loop_id="loop-1",
        round_id="round-1",
        decision_id="decision-1",
        action_id="action-1",
        target_context_id="context-1",
        target_context_revision_id="revision-1",
        message_id="message-1",
        content="Run the focused tests and fix only relevant failures.",
        content_hash="a" * 64,
        actor_kind="patrol",
        actor_id="patrol-1",
        grant_id="grant-1",
        grant_revision=1,
        goal_revision=1,
        status="created",
        idempotency_key="directive-key-1",
    )

    message = DelegatedDirectiveFactory.to_model_message(directive)

    assert message.type == "human"
    assert message.id == "message-1"
    assert message.content == directive.content
    assert message.additional_kwargs == {}
    assert message.response_metadata == {}
    assert "patrol" not in str(message.content).lower()
    assert "delegat" not in str(message.content).lower()


def test_delegated_message_reaches_provider_as_plain_user_message() -> None:
    directive = LoopDirective(
        directive_id="directive-provider",
        loop_id="loop-1",
        round_id="round-1",
        decision_id="decision-1",
        action_id="action-1",
        target_context_id="context-1",
        target_context_revision_id="revision-1",
        message_id="message-provider",
        content="Run only the focused integration tests.",
        content_hash="b" * 64,
        actor_kind="patrol",
        actor_id="patrol-1",
        grant_id="grant-1",
        grant_revision=1,
        goal_revision=1,
        status="created",
        idempotency_key="directive-provider-key",
    )
    captured: dict = {}
    response = {
        "id": "provider-response",
        "object": "chat.completion",
        "created": 0,
        "model": "contract-test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "done"},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def create_response(**payload):
        captured.update(payload)
        return SimpleNamespace(parse=lambda: response, headers={})

    model = ChatOpenAI(
        model="contract-test",
        api_key="test",
        base_url="https://example.test",
    )
    model.client = SimpleNamespace(
        with_raw_response=SimpleNamespace(create=create_response)
    )
    result = model.invoke([DelegatedDirectiveFactory.to_model_message(directive)])

    assert result.content == "done"
    assert captured["messages"] == [
        {"role": "user", "content": directive.content}
    ]
    assert "patrol" not in str(captured["messages"]).lower()
    assert "delegat" not in str(captured["messages"]).lower()


def test_patrol_cognitive_step_requires_reads_xor_decision() -> None:
    read = PatrolReadRequest(context_id="context-1", revision_id="revision-1")
    decision = PatrolDecisionProposal(
        rationale="The current Context is sufficient.",
        mission_references=({"role": "outcome", "reference_id": "outcome"},),
        actions=(
            {
                "action": "continue_context",
                "context_id": "context-1",
                "context_revision_id": "revision-1",
                "message": "Run the focused tests.",
            },
        ),
    )

    assert PatrolCognitiveStep(reads=(read,)).reads == (read,)
    assert PatrolCognitiveStep(decision=decision).decision == decision
    with pytest.raises(ValidationError):
        PatrolCognitiveStep()
    with pytest.raises(ValidationError):
        PatrolCognitiveStep(reads=(read,), decision=decision)


def test_patrol_proposal_references_mission_by_semantic_role() -> None:
    normal = PatrolDecisionProposal(
        rationale="继续实现最终结果。",
        mission_references=({"role": "boundary", "reference_id": "in_scope"},),
        actions=({"action": "wait_for_user", "reason": "需要用户输入"},),
    )
    completion = PatrolDecisionProposal(
        rationale="验证声明的完成检查。",
        mission_references=({"role": "completion_check", "reference_id": "tests"},),
        actions=({"action": "request_completion_verifier", "candidate_context_ids": ["context-1"]},),
    )
    observation = SimpleNamespace(mission={"completion_checks": [{"check_id": "tests"}]}, expansion_assessment=None)
    contract = PatrolDecisionContract()

    contract.validate(actions=normal.actions, mission_references=normal.mission_references, observation=observation)
    contract.validate(actions=completion.actions, mission_references=completion.mission_references, observation=observation)
    invalid = PatrolDecisionProposal(
        rationale="引用了不存在的检查。",
        mission_references=({"role": "completion_check", "reference_id": "invented"},),
        actions=({"action": "request_completion_verifier", "candidate_context_ids": ["context-1"]},),
    )
    with pytest.raises(Exception, match="稳定 check_id"):
        contract.validate(actions=invalid.actions, mission_references=invalid.mission_references, observation=observation)


def test_patrol_boundary_reference_is_a_closed_set() -> None:
    with pytest.raises(ValidationError):
        PatrolDecisionProposal(
            rationale="引用了未声明的 boundary 分组。",
            mission_references=({"role": "boundary", "reference_id": "boundary"},),
            actions=({"action": "wait_for_user", "reason": "需要用户输入"},),
        )
    with pytest.raises(ValidationError):
        PatrolDecisionProposal(
            rationale="outcome 引用不得自选取值。",
            mission_references=({"role": "outcome", "reference_id": "final"},),
            actions=({"action": "wait_for_user", "reason": "需要用户输入"},),
        )


def test_workspace_adoption_is_a_closed_patrol_action() -> None:
    action = PATROL_ACTION_ADAPTER.validate_python(
        {
            "action": "adopt_workspace_result",
            "source_slot_id": "slot-1",
            "source_revision": 3,
            "rationale": "Tests passed on the isolated result.",
        }
    )

    assert action.action == "adopt_workspace_result"
    with pytest.raises(ValidationError):
        PATROL_ACTION_ADAPTER.validate_python(
            {"action": "worker_commit_workspace", "source_slot_id": "slot-1"}
        )


def test_duration_and_no_progress_budgets_block_new_dispatch() -> None:
    guard = LoopBudgetGuard()

    exhausted = guard.evaluate(
        {"duration_seconds": 60, "no_progress_count": 0},
        {"max_duration_seconds": 60},
        "continue_context",
    )
    redirected = guard.evaluate(
        {"duration_seconds": 10, "no_progress_count": 3},
        {"max_duration_seconds": 60, "max_no_progress": 3},
        "continue_context",
    )

    assert exhausted.status == "exhausted"
    assert exhausted.reasons == ("duration_seconds_budget",)
    assert redirected.status == "change_direction"


def test_context_and_provider_budgets_are_explicit_hard_limits() -> None:
    budgets = LoopBudgetContract(max_contexts=2, max_providers=2).model_dump()
    guard = LoopBudgetGuard()

    contexts = guard.evaluate({"contexts": 2}, budgets, "create_lane")
    providers = guard.evaluate({"providers": 2}, budgets, "dispatch")

    assert contexts.status == "exhausted"
    assert contexts.reasons == ("contexts_budget",)
    assert providers.status == "exhausted"
    assert providers.reasons == ("providers_budget",)
    assert configured_provider_count(
        {
            "model_name": "main-model",
            "curator_model_name": "curator-model",
            "verifier_model_name": "main-model",
        }
    ) == 2


def test_user_intervention_preserves_external_scope() -> None:
    context = LoopInterventionRequest(
        mode="patrol_context_intent",
        context_id="context-1",
        content="保留测试方向，但停止扩展实现范围。",
    )
    portfolio = LoopInterventionRequest(
        mode="patrol_portfolio_intent",
        content="合并重复 Lane，并保留独立架构审查。",
    )

    assert context.scope() == "context"
    assert portfolio.scope() == "portfolio"
    assert "Patrol" not in context.content


def test_fact_projection_only_claims_explicit_test_counts() -> None:
    exact = LoopFactProjectionService._test_fact("12 passed, 2 failed, 1 skipped in 4.2s")
    unknown = LoopFactProjectionService._test_fact("pytest finished; inspect the report")
    unrelated = LoopFactProjectionService._test_fact("build finished with 1 failed target", "exec")

    assert exact == {
        "summary": "12 passed · 2 failed · 1 skipped",
        "metrics": {"passed": 12, "failed": 2, "skipped": 1, "count_status": "exact"},
        "status": "failed",
    }
    assert unknown["metrics"]["count_status"] == "unknown"
    assert unknown["status"] == "unknown"
    assert unrelated is None
