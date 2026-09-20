r"""本文件验证 Loop coordinator 单调 fencing、Kernel 提交时复验与 ownership-loss 终态。

输入为真实 PostgreSQL Loop、竞争 claim、过期 lease 和 Patrol intent；输出为旧 token 被拒、唯一 claim、重启后
token 增长及规范 interrupted 事件断言。具体工作流为创建最小 Loop，依次模拟释放、竞争、过期与 commit race。
示例：`pytest backend/tests/test_loop_fencing.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import AgentLoopService, LoopCoordinator, LoopCreateRequest, LoopKernel, PatrolDecisionIntent
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.models import AgentLoop, LoopCoordinatorLease, LoopRound
from backend.app.desktop.agent_loop.ownership import KernelFencingRejected, LoopFencingGuard
from backend.app.desktop.context_evolution import ContextRevisionContract, ContextRevisionOriginKind, ContextRevisionPayloadMode, ContextRevisionProjectionStatus, ContextRevisionRef, ContextRevisionRepository
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_monotonic_token_and_commit_time_rejection(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, context_id, revision_id = await _create_loop(sessions, tmp_path)
        coordinator = LoopCoordinator(sessions)
        try:
            first = await coordinator.claim("owner-a")
            assert first is not None
            assert await coordinator.release(first) is True
            second = await coordinator.claim("owner-b")
            assert second is not None
            assert int(second.fencing_token) > int(first.fencing_token)
            await coordinator.ownership_lost(first)
            async with sessions.begin() as session:
                with pytest.raises(KernelFencingRejected, match="superseded"):
                    await LoopFencingGuard().validate_current(session, first.round_id, int(first.fencing_token))
                superseded = await session.scalar(
                    select(LoopJournalEvent).where(
                        LoopJournalEvent.loop_id == snapshot["loop_id"],
                        LoopJournalEvent.kind == "patrol.session.superseded",
                    )
                )
                assert superseded is not None
            intent = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key=f"fenced:{second.round_id}",
                loop_id=second.loop_id,
                loop_revision=snapshot["revision"],
                round_id=second.round_id,
                holder_id=snapshot["holder_id"],
                grant_id=snapshot["grant"]["grant_id"],
                grant_revision=snapshot["authority_revision"],
                goal_revision=snapshot["goal_revision"],
                observed_frontier_hash=await _frontier(sessions, second.round_id),
                observed_workspace_revision=1,
                rationale="Continue with the current owner only.",
                actions=({"action": "continue_context", "context_id": context_id, "context_revision_id": revision_id, "message": "Continue."},),
                fencing_token=int(first.fencing_token),
            )
            kernel = LoopKernel(sessions, require_fencing=True)
            with pytest.raises(KernelFencingRejected):
                await kernel.commit(intent)
            committed = await kernel.commit(
                intent.model_copy(
                    update={
                        "decision_id": uuid.uuid4().hex,
                        "idempotency_key": f"current:{second.round_id}",
                        "fencing_token": int(second.fencing_token),
                    }
                )
            )
            assert committed.status == "committed"
            await coordinator.release(second)
        finally:
            loop = await service.get(snapshot["loop_id"])
            if loop["status"] in {"running", "paused", "waiting_user"}:
                await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


def test_duplicate_claim_restart_and_expiry_are_fenced(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, _ = await _create_loop(sessions, tmp_path)
        coordinator = LoopCoordinator(sessions, ttl_seconds=30)
        try:
            claims = await asyncio.gather(coordinator.claim("owner-a"), coordinator.claim("owner-b"))
            winner = next(item for item in claims if item is not None)
            assert sum(item is not None for item in claims) == 1
            async with sessions.begin() as session:
                await session.execute(
                    update(LoopCoordinatorLease)
                    .where(LoopCoordinatorLease.lease_id == winner.lease_id)
                    .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
            assert await coordinator.renew(winner) is False
            await coordinator.ownership_lost(winner)
            await coordinator.recover()
            replacement = await coordinator.claim("owner-c")
            assert replacement is not None
            assert int(replacement.fencing_token) > int(winner.fencing_token)
            async with sessions() as session:
                event = await session.scalar(
                    select(LoopJournalEvent)
                    .where(
                        LoopJournalEvent.loop_id == snapshot["loop_id"],
                        LoopJournalEvent.kind == "patrol.session.interrupted",
                    )
                )
                assert event is not None
                assert event.payload["reason"] == "lease_renewal_failed"
            await coordinator.release(replacement)
        finally:
            loop = await service.get(snapshot["loop_id"])
            if loop["status"] in {"running", "paused", "waiting_user"}:
                await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


async def _create_loop(sessions, tmp_path):
    suffix = uuid.uuid4().hex[:8]
    workspace_id = f"ws-fence-{suffix}"
    context_id = f"context-fence-{suffix}"
    revision_id = uuid.uuid4().hex
    loop_id = uuid.uuid4().hex
    workspace_path = tmp_path / workspace_id
    workspace_path.mkdir()
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name="fence"))
        await session.flush()
        session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{suffix}", title="fence"))
        await session.flush()
        ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=f"thread-{suffix}", checkpoint_ns="", checkpoint_id="checkpoint-1", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
        repository = ContextRevisionRepository()
        await repository.insert(session, ContextRevisionContract(ref=ref, content_hash="f" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.ROOT, created_at=datetime.now(UTC)))
        await repository.switch_current(session, ref, None)
        session.add(DesktopRun(run_id=f"initial-{suffix}", task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{suffix}", context_revision_id=revision_id, settled_at=datetime.now(UTC)))
    service = AgentLoopService(sessions)
    snapshot = await service.start(LoopCreateRequest(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, initial_run_id=f"initial-{suffix}", holder_id="patrol-fence", goal="Fence stale work", task_contract="Only current owner commits", acceptance_criteria=({"criterion_id": "done", "text": "current owner committed"},), capabilities=("continue_context", "request_completion"), context_scope=(context_id,), permission_scope=("read", "write")))
    return service, snapshot, context_id, revision_id


async def _frontier(sessions, round_id: str) -> str:
    async with sessions() as session:
        round_row = await session.get(LoopRound, round_id)
        return round_row.frontier_hash
