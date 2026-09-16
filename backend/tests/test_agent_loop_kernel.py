r"""本文件验证唯一 delegated authority、零 Worker 正常路径、纯 HumanMessage、用户抢占与 Completion Guard。

输入为真实 PostgreSQL Loop/Context revision、严格 Patrol intent 与 verifier evidence；输出为一次 Kernel
commit、外部 provenance、无模型来源标记、陈旧 decision superseded 和 unknown completion waiting-user
断言。具体工作流为 service start、Kernel commit、用户 override 和 guard 检查。示例：`pytest test_agent_loop_kernel.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import AgentLoopService, CompletionEvidenceService, CompletionGuard, CompletionVerificationContract, CriterionVerification, DelegatedDirectiveFactory, LoopCreateRequest, LoopKernel, PatrolDecisionIntent, PendingDecisionProjector
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDirective, LoopRound, LoopWorkerRequest, MessageProvenance
from backend.app.desktop.context_evolution import ContextRevisionContract, ContextRevisionOriginKind, ContextRevisionPayloadMode, ContextRevisionProjectionStatus, ContextRevisionRef, ContextRevisionRepository
from backend.app.desktop.models import DesktopThread, DesktopWorkspace
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
            service = AgentLoopService(sessions)
            snapshot = await service.start(LoopCreateRequest(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, holder_id="patrol-1", goal="Implement the feature", task_contract="Stay in scope and pass tests", acceptance_criteria=({"criterion_id": "tests", "text": "tests pass"},), capabilities=("continue_context", "request_completion_verifier", "request_completion", "wait_for_user", "stop_loop"), context_scope=(context_id,), permission_scope=("read", "write"), delegable_gates=("compression",)))
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
                workers = list((await session.scalars(select(LoopWorkerRequest).where(LoopWorkerRequest.loop_id == loop_id))).all())
                message = DelegatedDirectiveFactory.to_model_message(directive)
                assert message.content == directive.content
                assert message.id == directive.message_id
                assert message.additional_kwargs == {}
                assert provenance.source_kind == "delegated_patrol"
                assert workers == []
            overridden = await service.override(loop_id, "Prioritize migration safety", "Do not change public API", [{"criterion_id": "api", "text": "API stable"}])
            assert overridden["goal_revision"] == 2
            stale = intent.model_copy(update={"decision_id": uuid.uuid4().hex, "idempotency_key": "stale", "round_id": overridden["current_round_id"]})
            stale_result = await LoopKernel(sessions).commit(stale)
            assert stale_result.status == "superseded"
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
            service = AgentLoopService(sessions)
            snapshot = await service.start(LoopCreateRequest(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, holder_id="patrol-complete", goal="Finish safely", task_contract="Tests must pass", acceptance_criteria=({"criterion_id": "tests", "text": "tests pass", "required": True},), capabilities=("request_completion_verifier", "request_completion"), context_scope=(context_id,), permission_scope=("read",)))
            async with sessions.begin() as session:
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == workspace_id, WorkspaceSlot.kind == "authoritative"))
                worker_id = uuid.uuid4().hex
                session.add(LoopWorkerRequest(worker_request_id=worker_id, loop_id=loop_id, round_id=round_row.round_id, kind="completion_verifier", scope={"candidate_context_ids": [context_id]}))
            verification = CompletionVerificationContract(verification_id=uuid.uuid4().hex, loop_id=loop_id, round_id=round_row.round_id, goal_revision=1, frontier_hash=round_row.frontier_hash, workspace_revision=round_row.workspace_revision, criteria=(CriterionVerification(criterion_id="tests", status="satisfied", evidence=({"kind": "test_run", "status": "passed"},), explanation="Focused tests passed."),), conclusion="satisfied")
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
