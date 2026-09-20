r"""本文件验证唯一 delegated authority、零 Worker 正常路径、用户介入生命周期、Mission 修订与 Completion Guard。

输入为真实 PostgreSQL Loop/Context revision、严格 Patrol intent 与 verifier evidence；输出为一次 Kernel
commit、外部 provenance、Mission 不变、直接消息 delivery/Run 终态、陈旧 decision superseded 和 unknown completion waiting-user 断言。
具体工作流为 service start、Kernel commit、直接消息交付与结算、用户确认 Mission revision 和 guard 检查。
示例：`pytest test_agent_loop_kernel.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from types import SimpleNamespace
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import AgentLoopService, CompletionEvidenceService, CompletionGuard, CompletionVerificationContract, CriterionVerification, DelegatedDirectiveFactory, LoopCoordinator, LoopCreateRequest, LoopKernel, LoopWaveDispatcher, PatrolDecisionIntent, PendingDecisionProjector
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.mission_contract import CompletionCheckDefinition, ExecutionBoundaries, LoopMissionContract
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_history import MissionHistoryQueryService
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDirective, LoopDirectiveTransition, LoopGoalRevision, LoopInterventionTransition, LoopRound, LoopUserIntent, LoopWorkerRequest, MessageProvenance
from backend.app.desktop.context_evolution import ContextRevisionContract, ContextRevisionOriginKind, ContextRevisionPayloadMode, ContextRevisionProjectionStatus, ContextRevisionRef, ContextRevisionRepository
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_closed_action_union_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        PatrolDecisionIntent.model_validate({"decision_id": "d", "idempotency_key": "k", "loop_id": "l", "loop_revision": 1, "round_id": "r", "holder_id": "h", "grant_id": "g", "grant_revision": 1, "goal_revision": 1, "observed_frontier_hash": "a" * 64, "observed_workspace_revision": 1, "rationale": "x", "actions": [{"action": "worker_commit_database"}]})


def test_zero_worker_delegated_directive_and_user_override(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-loop-{suffix}"
        context_id = f"context-loop-{suffix}"
        revision_id = uuid.uuid4().hex
        loop_id = uuid.uuid4().hex
        workspace_path = tmp_path / workspace_id
        workspace_path.mkdir()
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name="loop"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{suffix}", title="loop"))
                await session.flush()
                ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=f"thread-{suffix}", checkpoint_ns="", checkpoint_id="checkpoint-1", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
                await ContextRevisionRepository().insert(session, ContextRevisionContract(ref=ref, content_hash="a" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.ROOT, created_at=datetime.now(UTC)))
                await ContextRevisionRepository().switch_current(session, ref, None)
                session.add(DesktopRun(run_id=f"initial-{suffix}", task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{suffix}", context_revision_id=revision_id, settled_at=datetime.now(UTC)))
            service = AgentLoopService(sessions)
            snapshot = await service.start(LoopCreateRequest(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, initial_run_id=f"initial-{suffix}", holder_id="patrol-1", goal="Implement the feature", task_contract="Stay in scope and pass tests", acceptance_criteria=({"criterion_id": "tests", "text": "tests pass"},), capabilities=("continue_context", "request_completion_verifier", "request_completion", "wait_for_user", "stop_loop"), context_scope=(context_id,), permission_scope=("read", "write"), delegable_gates=("compression",)))
            compression = await PendingDecisionProjector(sessions).project(loop_id, {"type": "compression", "checkpoint_id": "checkpoint-1"})
            assert compression.delegable is True
            assert (await service.get(loop_id))["status"] == "running"
            intent = PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key="decision-1", loop_id=loop_id, loop_revision=snapshot["revision"], round_id=snapshot["current_round_id"], holder_id="patrol-1", grant_id=snapshot["grant"]["grant_id"], grant_revision=1, goal_revision=1, observed_frontier_hash=(await _round_frontier(sessions, snapshot["current_round_id"])), observed_workspace_revision=1, rationale="Database work is complete; run tests next.", actions=({"action": "continue_context", "context_id": context_id, "context_revision_id": revision_id, "message": "Run the database tests and fix only relevant failures."},))
            result = await LoopKernel(sessions).commit(intent)
            again = await LoopKernel(sessions).commit(intent)
            assert result == again
            assert result.status == "committed"
            async with sessions() as session:
                directive = await session.get(LoopDirective, result.directive_ids[0])
                provenance = await session.scalar(select(MessageProvenance).where(MessageProvenance.directive_id == directive.directive_id))
                transitions = tuple((await session.scalars(select(LoopDirectiveTransition).where(LoopDirectiveTransition.directive_id == directive.directive_id).order_by(LoopDirectiveTransition.revision))).all())
                journal = tuple((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.entity_type == "directive", LoopJournalEvent.entity_id == directive.directive_id).order_by(LoopJournalEvent.sequence))).all())
                workers = list((await session.scalars(select(LoopWorkerRequest).where(LoopWorkerRequest.loop_id == loop_id))).all())
                message = DelegatedDirectiveFactory.to_model_message(directive)
                assert message.content == directive.content
                assert message.id == directive.message_id
                assert message.additional_kwargs == {}
                assert provenance.source_kind == "delegated_patrol"
                assert [item.to_state for item in transitions] == ["proposed", "authorized"]
                assert [item.kind for item in journal] == ["directive.proposed", "directive.authorized"]
                assert workers == []
            mission_before = snapshot["mission"]
            direct = await service.user_message(context_id, "只复现一次 Windows 路径失败，不改变长期目标。")
            after_direct = await service.get(loop_id)
            assert direct["mission_revision"] == 1
            assert after_direct["goal_revision"] == 1
            assert after_direct["mission"] == mission_before
            async with sessions() as session:
                revisions = list((await session.scalars(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == loop_id))).all())
                user_intent = await session.get(LoopUserIntent, direct["intent_id"])
                direct_history = tuple((await session.scalars(select(LoopInterventionTransition).where(LoopInterventionTransition.intent_id == user_intent.intent_id).order_by(LoopInterventionTransition.revision))).all())
                cancelled_directive = await session.get(LoopDirective, result.directive_ids[0])
                cancelled_history = tuple((await session.scalars(select(LoopDirectiveTransition).where(LoopDirectiveTransition.directive_id == cancelled_directive.directive_id).order_by(LoopDirectiveTransition.revision))).all())
                assert len(revisions) == 1
                assert user_intent.status == "addressed"
                assert user_intent.origin_kind == "user"
                assert user_intent.delivery_state == "accepted"
                assert [item.to_state for item in direct_history] == ["submitted", "accepted"]
                assert cancelled_directive.lifecycle_state == "cancelled"
                assert cancelled_history[-1].to_state == "cancelled"
                assert user_intent.content == "只复现一次 Windows 路径失败，不改变长期目标。"
            direct_run_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(DesktopRun(run_id=direct_run_id, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{suffix}", context_revision_id=revision_id, loop_id=loop_id, round_id=direct["round_id"], user_intent_id=direct["intent_id"], settled_at=datetime.now(UTC)))
            await service.bind_user_message_run(direct["intent_id"], direct_run_id)
            async with sessions.begin() as session:
                await LoopCoordinator(sessions).handle_run_settled(SimpleNamespace(event_id=uuid.uuid4().hex, run_id=direct_run_id, payload={}), session)
            async with sessions() as session:
                settled_history = tuple((await session.scalars(select(LoopInterventionTransition).where(LoopInterventionTransition.intent_id == direct["intent_id"]).order_by(LoopInterventionTransition.revision))).all())
            assert [item.to_state for item in settled_history] == ["submitted", "accepted", "delivered", "run_started", "settled"]
            overridden = await service.override(loop_id, "Prioritize migration safety", "Do not change public API", [{"criterion_id": "api", "text": "API stable"}])
            assert overridden["goal_revision"] == 2
            mission_event = next(event for event in await service.events(loop_id) if event["type"] == "MissionRevisionActivated")
            assert mission_event["payload"]["previous_mission_revision"] == 1
            assert mission_event["payload"]["mission_revision"] == 2
            stale = intent.model_copy(update={"decision_id": uuid.uuid4().hex, "idempotency_key": "stale", "round_id": overridden["current_round_id"]})
            stale_result = await LoopKernel(sessions).commit(stale)
            assert stale_result.status == "superseded"
            assert stale_result.reason == "mission_revision_changed"
            restricted = await service.override(
                loop_id,
                mission=LoopMissionContract(
                    outcome="Finish the migration safely",
                    boundaries=ExecutionBoundaries(prohibited_actions=("continue_context",)),
                    completion_checks=(CompletionCheckDefinition(check_id="api", claim="API remains stable", expected_evidence_kinds=("test",)),),
                ),
            )
            blocked = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key="blocked-by-mission",
                loop_id=loop_id,
                loop_revision=restricted["revision"],
                round_id=restricted["current_round_id"],
                holder_id="patrol-1",
                grant_id=restricted["grant"]["grant_id"],
                grant_revision=restricted["authority_revision"],
                goal_revision=restricted["goal_revision"],
                observed_frontier_hash=await _round_frontier(sessions, restricted["current_round_id"]),
                observed_workspace_revision=1,
                rationale="Continue even though the current Mission prohibits it.",
                actions=({"action": "continue_context", "context_id": context_id, "context_revision_id": revision_id, "message": "Continue."},),
            )
            blocked_result = await LoopKernel(sessions).commit(blocked)
            assert blocked_result.status == "rejected"
            assert blocked_result.reason == "boundary:prohibited_actions:continue_context"
            async with sessions() as session:
                rejected = await session.get(LoopDirective, blocked_result.directive_ids[0])
                rejected_history = tuple((await session.scalars(select(LoopDirectiveTransition).where(LoopDirectiveTransition.directive_id == rejected.directive_id).order_by(LoopDirectiveTransition.revision))).all())
            assert rejected.status == "blocked"
            assert rejected.lifecycle_state == "rejected"
            assert [item.to_state for item in rejected_history] == ["proposed", "rejected"]

            async def forbidden_launch(*_args):
                raise AssertionError("rejected directive must never launch")

            assert await LoopWaveDispatcher(sessions, forbidden_launch).dispatch(loop_id, restricted["current_round_id"], 1) == ()

            target_round = await service.override(
                loop_id,
                mission=LoopMissionContract(
                    outcome="Continue safely",
                    boundaries=ExecutionBoundaries(),
                    completion_checks=(CompletionCheckDefinition(check_id="api", claim="API remains stable", expected_evidence_kinds=("test",)),),
                ),
            )
            missing_target = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key="missing-target",
                loop_id=loop_id,
                loop_revision=target_round["revision"],
                round_id=target_round["current_round_id"],
                holder_id="patrol-1",
                grant_id=target_round["grant"]["grant_id"],
                grant_revision=target_round["authority_revision"],
                goal_revision=target_round["goal_revision"],
                observed_frontier_hash=await _round_frontier(sessions, target_round["current_round_id"]),
                observed_workspace_revision=1,
                rationale="Target disappeared before authorization.",
                actions=({"action": "continue_context", "context_id": "missing-context", "context_revision_id": "missing-revision", "message": "Continue."},),
            )
            missing_result = await LoopKernel(sessions).commit(missing_target)
            assert missing_result.status == "rejected"
            async with sessions() as session:
                missing_events = tuple((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.entity_type == "directive", LoopJournalEvent.entity_id == missing_result.directive_ids[0]))).all())
            assert len(missing_events) == 1
            assert missing_events[0].kind == "directive.rejected"
            assert missing_events[0].payload["target_context_id"] == "missing-context"
            async with sessions.begin() as session:
                session.add(LoopGoalRevision(goal_revision_id=uuid.uuid4().hex, loop_id=loop_id, revision=99, goal="Legacy archived outcome", task_contract="原始 Task Contract，不自动拆分。", acceptance_criteria=[{"criterion_id": "legacy", "text": "legacy evidence"}], authored_by="legacy-import"))
            async with sessions() as session:
                history = await MissionHistoryQueryService().read(session, loop_id)
            assert sum(item["active"] for item in history["revisions"]) == 1
            assert next(item for item in history["revisions"] if item["active"])["revision"] == target_round["goal_revision"]
            legacy = next(item for item in history["revisions"] if item["kind"] == "legacy_goal_contract")
            assert legacy["task_contract"] == "原始 Task Contract，不自动拆分。"
            assert "boundaries" not in legacy
            access = await PendingDecisionProjector(sessions).project(loop_id, {"type": "access_approval", "path": "outside-workspace"})
            assert access.delegable is False
            assert (await service.get(loop_id))["status"] == "waiting_user"
        finally:
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
            if loop is not None and loop.status in {"running", "paused", "waiting_user"}:
                await service.control(loop_id, "stop")
            await engine.dispose()

    asyncio.run(run())


async def _round_frontier(sessions, round_id: str) -> str:
    from backend.app.desktop.agent_loop.models import LoopRound
    async with sessions() as session:
        return (await session.get(LoopRound, round_id)).frontier_hash


def test_completion_guard_rejects_unknown() -> None:
    verification = CompletionVerificationContract(verification_id="v", loop_id="l", round_id="r", goal_revision=1, frontier_hash="a" * 64, workspace_revision=1, criteria=(CriterionVerification(criterion_id="tests", status="unknown", explanation="No fresh test run"),), conclusion="unknown", unresolved=("tests",))
    result = CompletionGuard().check(verification, goal_revision=1, frontier_hash="a" * 64, workspace_revision=1, active_or_queued_runs=0, pending_human_gates=0, portfolio_published=True, workspace_adopted=True, grant_allows_completion=True)
    assert result.allowed is False
    assert result.waiting_user is True


def test_independent_verifier_is_required_before_completion_and_final_result_is_auditable(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-complete-{suffix}"
        context_id = f"context-complete-{suffix}"
        revision_id = uuid.uuid4().hex
        loop_id = uuid.uuid4().hex
        workspace_path = tmp_path / workspace_id
        workspace_path.mkdir()
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name="complete"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{suffix}", title="complete"))
                await session.flush()
                ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=f"thread-{suffix}", checkpoint_ns="", checkpoint_id="checkpoint-1", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
                repository = ContextRevisionRepository()
                await repository.insert(session, ContextRevisionContract(ref=ref, content_hash="b" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.ROOT, created_at=datetime.now(UTC)))
                await repository.switch_current(session, ref, None)
                session.add(DesktopRun(run_id=f"initial-{suffix}", task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{suffix}", context_revision_id=revision_id, settled_at=datetime.now(UTC)))
            service = AgentLoopService(sessions)
            snapshot = await service.start(LoopCreateRequest(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, initial_run_id=f"initial-{suffix}", holder_id="patrol-complete", goal="Finish safely", task_contract="Tests must pass", acceptance_criteria=({"criterion_id": "tests", "text": "tests pass", "required": True},), capabilities=("request_completion_verifier", "request_completion"), context_scope=(context_id,), permission_scope=("read",)))
            async with sessions.begin() as session:
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == workspace_id, WorkspaceSlot.kind == "authoritative"))
                worker_id = uuid.uuid4().hex
                session.add(LoopWorkerRequest(worker_request_id=worker_id, loop_id=loop_id, round_id=round_row.round_id, kind="completion_verifier", scope={"candidate_context_ids": [context_id]}))
            verification = CompletionVerificationContract(verification_id=uuid.uuid4().hex, loop_id=loop_id, round_id=round_row.round_id, goal_revision=1, frontier_hash=round_row.frontier_hash, workspace_revision=round_row.workspace_revision, criteria=(CriterionVerification(criterion_id="tests", status="satisfied", evidence=({"kind": "fact", "source_id": f"initial-{suffix}", "summary": "Focused tests passed."},), explanation="Focused tests passed."),), conclusion="satisfied")
            await CompletionEvidenceService(sessions).record(verification, worker_id)
            intent = PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key=f"complete-{suffix}", loop_id=loop_id, loop_revision=snapshot["revision"], round_id=round_row.round_id, holder_id="patrol-complete", grant_id=snapshot["grant"]["grant_id"], grant_revision=1, goal_revision=1, observed_frontier_hash=round_row.frontier_hash, observed_workspace_revision=round_row.workspace_revision, rationale="Independent evidence satisfies every required criterion.", actions=({"action": "request_completion", "verification_id": verification.verification_id, "final_context_ids": [context_id], "final_slot_id": slot.slot_id},))
            result = await LoopKernel(sessions).commit(intent)
            assert result.status == "committed"
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
                assert loop.status == "completed"
                assert loop.final_result["verification"]["verification_id"] == verification.verification_id
                assert loop.final_result["final_path"][0]["context_id"] == context_id
                assert loop.final_result["workspace"]["authoritative_slot_id"] == slot.slot_id
                assert loop.final_result["delegated_directives"] == []
        finally:
            await engine.dispose()

    asyncio.run(run())
