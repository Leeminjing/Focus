r"""本文件对外提供单来源 Context recovery 合同、可信编译与 Patrol identity 边界测试。

输入为冻结 revision、带早期约束的正常/完整/中断/孤立 Tool Exchange 消息、恢复 authority 快照及模型动作 payload；输出为稳定
opportunity identity、协议合法 Lane plan、逐项 freshness 错误码、显式等待原因、可追溯来源和严格 action 拒绝断言。具体工作流为纯内存构造
authoritative source，编译恢复计划并交给通用 Lane compiler 和 authority validator，再检查 Patrol 只能引用 observation 暴露的
opportunity，并确认重复发现保留首次到期时间。示例：`pytest backend/tests/test_context_recovery.py -q`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.app.desktop.agent_loop.context_recovery import (
    ContextRecoveryAuthorityError,
    ContextRecoveryAuthorityValidator,
    ContextRecoveryOpportunityContract,
    ContextRecoveryOpportunityService,
    ContextRecoveryOpportunityRepository,
    ContextRecoveryPlanCompiler,
)
from backend.app.desktop.agent_loop.patrol import PatrolContractViolation
from backend.app.desktop.agent_loop.patrol_contract import PatrolDecisionContract
from backend.app.desktop.agent_loop.schemas import (
    LoopObservationEnvelope,
    PATROL_MODEL_ACTION_ADAPTER,
)
from backend.app.desktop.context_curation import (
    MultiSourceEvidence,
    NamespacedMessageRef,
    SourceMessageEvidence,
    SourceRevisionEvidence,
    compile_lane,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionPayloadMode,
    ContextRevisionRef,
)


def _source() -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id="context-1",
        revision_id="revision-1",
        generation=1,
        execution_thread_id="thread-1",
        checkpoint_id="checkpoint-1",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )


def _interrupted_messages() -> tuple[dict, ...]:
    return (
        {"id": "constraint", "role": "human", "content": "Preserve the public interface and permission boundary."},
        {
            "id": "caller",
            "role": "ai",
            "content": "I will inspect the tests.",
            "tool_calls": [{"id": "call-1", "name": "read_file", "args": {"path": "tests.py"}}],
        },
    )


def _evidence(source: ContextRevisionRef, messages: tuple[dict, ...]) -> MultiSourceEvidence:
    return MultiSourceEvidence(
        sources=(
            SourceRevisionEvidence(
                source=source,
                projection_hash="a" * 64,
                content_hash="b" * 64,
                messages=tuple(
                    SourceMessageEvidence(
                        ref=NamespacedMessageRef(source=source, message_id=message["id"]),
                        role=message["role"],
                        content=message.get("content", ""),
                        tool_calls=tuple(message.get("tool_calls") or ()),
                        tool_call_id=message.get("tool_call_id"),
                        name=message.get("name"),
                        status=message.get("status"),
                    )
                    for message in messages
                ),
            ),
        )
    )


def _observation(opportunity_id: str) -> LoopObservationEnvelope:
    return LoopObservationEnvelope(
        loop_id="loop-1",
        loop_revision=1,
        round_id="round-1",
        goal_revision=1,
        authority_revision=1,
        observed_frontier_hash="f" * 64,
        mission={"outcome": "Recover safely", "completion_checks": []},
        grant={"grant_id": "grant-1", "revision": 1},
        portfolio_frontier=(),
        workspace={"revision": 1},
        budget={},
        recovery_opportunities=({"opportunity_id": opportunity_id},),
    )


def test_recovery_compiler_preserves_early_constraint_and_closes_dangling_exchange_without_tool_success() -> None:
    source = _source()
    messages = _interrupted_messages()

    plan = ContextRecoveryPlanCompiler().compile(
        source,
        messages,
        run_id="run-1",
        reason="provider rejected insufficient tool messages",
    )
    candidate = compile_lane(plan, _evidence(source, messages))

    assert candidate.projection_status == "valid"
    assert candidate.execution_messages[0]["content"] == messages[0]["content"]
    assert not candidate.execution_messages[-1].get("tool_calls")
    assert "未产生可证明的工具结果" in candidate.execution_messages[-1]["content"]
    assert {ref.message_id for ref in candidate.message_lineage[-1].sources} == {"caller"}
    assert plan.lane_policy["recovery_protocol_policy"] == "exclude_terminal_unresolved_exchange_v2"


def test_recovery_compiler_rebuilds_complete_exchange_and_rejects_orphan_result() -> None:
    source = _source()
    complete = (
        *_interrupted_messages(),
        {
            "id": "result",
            "role": "tool",
            "content": "file contents",
            "tool_call_id": "call-1",
            "name": "read_file",
            "status": "success",
        },
    )

    plan = ContextRecoveryPlanCompiler().compile(source, complete, run_id="run-1", reason="stopped later")
    candidate = compile_lane(plan, _evidence(source, complete))

    assert [message["role"] for message in candidate.execution_messages] == ["human", "ai", "tool"]
    assert candidate.execution_messages[-1]["status"] == "success"
    with pytest.raises(ValueError, match="孤立 ToolMessage"):
        ContextRecoveryPlanCompiler().compile(
            source,
            ({"id": "orphan", "role": "tool", "content": "x", "tool_call_id": "missing", "name": "read"},),
            run_id="run-2",
            reason="unknown",
        )


def test_recovery_compiler_rejects_nonterminal_or_partially_resolved_exchange() -> None:
    source = _source()
    with pytest.raises(ValueError, match="非终端未闭合"):
        ContextRecoveryPlanCompiler().compile(
            source,
            (*_interrupted_messages(), {"id": "later", "role": "human", "content": "Continue."}),
            run_id="run-2",
            reason="provider rejected",
        )
    partial = (
        _interrupted_messages()[0],
        {
            "id": "caller",
            "role": "ai",
            "content": "",
            "tool_calls": [
                {"id": "call-1", "name": "read_file", "args": {}},
                {"id": "call-2", "name": "write_file", "args": {}},
            ],
        },
        {"id": "result", "role": "tool", "content": "contents", "tool_call_id": "call-1", "name": "read_file"},
    )
    with pytest.raises(ValueError, match="部分工具结果"):
        ContextRecoveryPlanCompiler().compile(source, partial, run_id="run-2", reason="provider rejected")


def test_recovery_opportunity_identity_and_serialization_are_stable() -> None:
    source = _source()
    plan = ContextRecoveryPlanCompiler().compile(source, _interrupted_messages(), run_id="run-1", reason="interrupted")
    values = dict(
        loop_id="loop-1",
        source=source,
        source_frontier_hash="f" * 64,
        goal_revision=2,
        workspace_revision=3,
        authority_revision=4,
        grant_id="grant-1",
        grant_revision=4,
        source_run_id="run-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        safe_summary="Recover one authoritative source.",
        plan=plan,
    )

    left = ContextRecoveryOpportunityContract.create(**values)
    right = ContextRecoveryOpportunityContract.model_validate(left.model_dump(mode="json"))

    assert left == right
    assert left.opportunity_id == ContextRecoveryOpportunityContract.create(**values).opportunity_id
    assert set(left.public_payload()) == {
        "opportunity_id",
        "source_context_id",
        "source_revision_id",
        "source_run_id",
        "compiler_version",
        "expires_at",
        "safe_summary",
    }


def test_repeated_recovery_discovery_keeps_the_original_expiry() -> None:
    source = _source()
    plan = ContextRecoveryPlanCompiler().compile(
        source, _interrupted_messages(), run_id="run-1", reason="interrupted"
    )
    values = dict(
        loop_id="loop-1",
        source=source,
        source_frontier_hash="f" * 64,
        goal_revision=2,
        workspace_revision=3,
        authority_revision=4,
        grant_id="grant-1",
        grant_revision=4,
        source_run_id="run-1",
        safe_summary="Recover one authoritative source.",
        plan=plan,
    )
    first = ContextRecoveryOpportunityContract.create(
        **values, expires_at=datetime(2026, 9, 24, tzinfo=UTC) + timedelta(hours=1)
    )
    repeated = ContextRecoveryOpportunityContract.create(
        **values, expires_at=datetime(2026, 9, 24, tzinfo=UTC) + timedelta(hours=2)
    )
    row = SimpleNamespace(payload=first.model_dump(mode="json"), round_id="first-round")

    class Session:
        async def get(self, model, identity, **kwargs):
            assert identity == first.opportunity_id
            return row

    returned = asyncio.run(
        ContextRecoveryOpportunityRepository().put(Session(), repeated, "second-round")
    )

    assert first.opportunity_id == repeated.opportunity_id
    assert returned is row
    assert ContextRecoveryOpportunityContract.model_validate(row.payload) == first
    assert row.round_id == "first-round"


def test_patrol_recovery_action_is_identity_only_and_must_reference_observation() -> None:
    opportunity_id = "a" * 64
    action = PATROL_MODEL_ACTION_ADAPTER.validate_python(
        {"action": "recover_context", "opportunity_id": opportunity_id}
    )

    assert action.model_dump() == {"action": "recover_context", "opportunity_id": opportunity_id}
    with pytest.raises(ValidationError):
        PATROL_MODEL_ACTION_ADAPTER.validate_python(
            {"action": "recover_context", "opportunity_id": opportunity_id, "plan": {"source": "invented"}}
        )
    PatrolDecisionContract().validate(
        actions=(action,),
        mission_references=(),
        observation=_observation(opportunity_id),
    )
    with pytest.raises(PatrolContractViolation, match="observation 之外"):
        PatrolDecisionContract().validate(
            actions=(action,),
            mission_references=(),
            observation=_observation("b" * 64),
        )


@pytest.mark.parametrize(
    ("case", "expected_code"),
    (
        ("consumed", "opportunity_consumed"),
        ("expired", "opportunity_expired"),
        ("compiler", "compiler_version_changed"),
        ("frontier", "frontier_changed"),
        ("goal", "goal_changed"),
        ("workspace", "workspace_changed"),
        ("authority", "authority_changed"),
        ("grant", "grant_changed"),
        ("grant_revision", "grant_revision_changed"),
        ("source", "source_revision_changed"),
        ("plan", "recovery_plan_changed"),
    ),
)
def test_recovery_authority_validator_rejects_every_stale_binding(
    case: str,
    expected_code: str,
) -> None:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    source = _source()
    plan = ContextRecoveryPlanCompiler().compile(
        source,
        _interrupted_messages(),
        run_id="run-1",
        reason="interrupted",
    )
    opportunity = ContextRecoveryOpportunityContract.create(
        loop_id="loop-1",
        source=source,
        source_frontier_hash="f" * 64,
        goal_revision=2,
        workspace_revision=3,
        authority_revision=4,
        grant_id="grant-1",
        grant_revision=4,
        source_run_id="run-1",
        expires_at=now + timedelta(hours=1),
        safe_summary="Recover one authoritative source.",
        plan=plan,
    )
    trusted_plan = plan.model_copy(
        update={
            "lane_policy": {
                **plan.lane_policy,
                "recovery_opportunity_id": opportunity.opportunity_id,
            }
        }
    )
    arguments = {
        "loop_id": "loop-1",
        "goal_revision": 2,
        "workspace_revision": 3,
        "authority_revision": 4,
        "grant_id": "grant-1",
        "grant_revision": 4,
        "frontier_hash": "f" * 64,
        "current_source_revision_id": source.revision_id,
        "plan": trusted_plan,
        "now": now,
    }
    if case == "consumed":
        opportunity = opportunity.model_copy(update={"status": "consumed"})
    elif case == "expired":
        opportunity = opportunity.model_copy(update={"expires_at": now - timedelta(seconds=1)})
    elif case == "compiler":
        opportunity = opportunity.model_copy(update={"compiler_version": "legacy-recovery-v0"})
    elif case == "frontier":
        arguments["frontier_hash"] = "e" * 64
    elif case == "goal":
        arguments["goal_revision"] = 3
    elif case == "workspace":
        arguments["workspace_revision"] = 4
    elif case == "authority":
        arguments["authority_revision"] = 5
    elif case == "grant":
        arguments["grant_id"] = "grant-2"
    elif case == "grant_revision":
        arguments["grant_revision"] = 5
    elif case == "source":
        arguments["current_source_revision_id"] = "revision-2"
    elif case == "plan":
        arguments["plan"] = trusted_plan.model_copy(update={"purpose": "Invented recovery purpose"})

    with pytest.raises(ContextRecoveryAuthorityError) as raised:
        ContextRecoveryAuthorityValidator().validate(opportunity, **arguments)

    assert raised.value.code == expected_code


def test_recovery_authority_validator_accepts_the_exact_frozen_plan() -> None:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    source = _source()
    plan = ContextRecoveryPlanCompiler().compile(
        source,
        _interrupted_messages(),
        run_id="run-1",
        reason="interrupted",
    )
    opportunity = ContextRecoveryOpportunityContract.create(
        loop_id="loop-1",
        source=source,
        source_frontier_hash="f" * 64,
        goal_revision=2,
        workspace_revision=3,
        authority_revision=4,
        grant_id="grant-1",
        grant_revision=4,
        source_run_id="run-1",
        expires_at=now + timedelta(hours=1),
        safe_summary="Recover one authoritative source.",
        plan=plan,
    )
    trusted_plan = plan.model_copy(
        update={
            "lane_policy": {
                **plan.lane_policy,
                "recovery_opportunity_id": opportunity.opportunity_id,
            }
        }
    )

    ContextRecoveryAuthorityValidator().validate(
        opportunity,
        loop_id="loop-1",
        goal_revision=2,
        workspace_revision=3,
        authority_revision=4,
        grant_id="grant-1",
        grant_revision=4,
        frontier_hash="f" * 64,
        current_source_revision_id=source.revision_id,
        plan=trusted_plan,
        now=now,
    )


def test_ambiguous_protocol_history_returns_explicit_waiting_reason() -> None:
    class Repository:
        @staticmethod
        async def pending_for_round(session, round_id):
            return ()

    async def run():
        service = ContextRecoveryOpportunityService(reader=None)
        service._repository = Repository()
        return await service.discover(
            None,
            loop=SimpleNamespace(loop_id="loop-1"),
            round_row=SimpleNamespace(round_id="round-1"),
            grant=SimpleNamespace(),
            workspace_revision=1,
            frontier=(
                {
                    "context_id": "context-1",
                    "revision_id": "revision-needing-approval",
                    "projection_status": "approval_required",
                    "revision": _source().model_dump(mode="json"),
                },
            ),
            runs=(),
        )

    discovery = asyncio.run(run())

    assert discovery.opportunities == ()
    assert "缺少可证明的中断因果" in discovery.waiting_reason
    assert "revision-needing-approval" in discovery.waiting_reason
