r"""本文件验证发布队列、Context Run 排队、Curator 重试与 pause 收敛。

输入为真实 PostgreSQL Loop、阻塞发布、耗尽的 Context 容量、失败 Worker 和 pause 控制；输出为独立进度、
持久 queued_reason、有界 attempt identity 及全部活动工作终态断言。具体工作流为创建最小 Loop 后逐一驱动
三个独立运行路径，以提交后的完成事件同步 Worker，再从持久实体读取结果。示例：`pytest backend/tests/test_loop_runtime_pools.py`。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop import (
    AgentLoopService,
    ContextRunPool,
    LoopCoordinator,
    LoopCreateRequest,
    LoopKernel,
    PatrolDecisionIntent,
)
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopDecision,
    LoopDirective,
    LoopRound,
    LoopWorkerRequest,
)
from backend.app.desktop.agent_loop.publication_queue import (
    LoopPortfolioPublicationQueue,
)
from backend.app.desktop.agent_loop.workers import LoopWorkerRuntime
from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_slow_publication_runs_in_its_own_durable_queue(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, _ = await _create_loop(sessions, tmp_path)
        started = asyncio.Event()
        release = asyncio.Event()

        class Publisher:
            async def publish(self, decision_id: str):
                started.set()
                await release.wait()
                async with sessions.begin() as session:
                    decision = await session.get(LoopDecision, decision_id, with_for_update=True)
                    round_row = await session.get(LoopRound, decision.round_id, with_for_update=True)
                    decision.status = "committed"
                    round_row.status = "settled"

        decision_id = uuid.uuid4().hex
        async with sessions.begin() as session:
            round_row = await session.get(LoopRound, snapshot["current_round_id"], with_for_update=True)
            round_row.status = "publishing"
            round_row.decision_id = decision_id
            session.add(LoopDecision(decision_id=decision_id, loop_id=snapshot["loop_id"], round_id=round_row.round_id, holder_id=snapshot["holder_id"], intent={"actions": []}, rationale="publish", status="publishing", idempotency_key=f"publish:{decision_id}"))
        queue = LoopPortfolioPublicationQueue(sessions, Publisher(), concurrency=1)
        try:
            assert await queue.drain() == 1
            await asyncio.wait_for(started.wait(), timeout=1)
            async with sessions() as session:
                decision = await session.get(LoopDecision, decision_id)
                assert decision.status == "publishing_run"
                assert decision.deferred_attempt == 1
            release.set()
            status = "publishing_run"
            for _ in range(50):
                await queue.drain()
                async with sessions() as session:
                    status = (await session.get(LoopDecision, decision_id)).status
                if status == "committed":
                    break
                await asyncio.sleep(0.01)
            assert status == "committed"
        finally:
            release.set()
            await queue.close()
            loop = await service.get(snapshot["loop_id"])
            if loop["status"] in {"running", "paused", "waiting_user"}:
                await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


def test_context_capacity_is_visible_and_pause_converges_work(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, context_id, revision_id = await _create_loop(sessions, tmp_path)
        try:
            round_id = snapshot["current_round_id"]
            intent = PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key=f"directive:{round_id}", loop_id=snapshot["loop_id"], loop_revision=snapshot["revision"], round_id=round_id, holder_id=snapshot["holder_id"], grant_id=snapshot["grant"]["grant_id"], grant_revision=1, goal_revision=1, observed_frontier_hash=await _frontier(sessions, round_id), observed_workspace_revision=1, rationale="Queue one Context.", actions=({"action": "continue_context", "context_id": context_id, "context_revision_id": revision_id, "message": "Continue."},))
            result = await LoopKernel(sessions).commit(intent)
            counter = ContextRunPool(sessions, LoopCoordinator(sessions), object())
            baseline_active = await counter._active_run_count()
            active_run_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(DesktopRun(run_id=active_run_id, task_id=context_id, agent_id=f"worker:{context_id}", kind="teammate", status="running", origin="delegated_patrol", execution_thread_id=f"capacity-{uuid.uuid4().hex}", loop_id=snapshot["loop_id"], round_id=round_id))
                session.add(LoopWorkerRequest(worker_request_id=uuid.uuid4().hex, loop_id=snapshot["loop_id"], round_id=round_id, kind="lane_curator", scope={}, status="running"))
            pool = ContextRunPool(
                sessions,
                LoopCoordinator(sessions),
                object(),
                concurrency=baseline_active + 1,
            )
            assert await pool.drain() == 0
            async with sessions() as session:
                directive = await session.get(LoopDirective, result.directive_ids[0])
                assert directive.queued_reason == "global_context_capacity"
            launched = asyncio.Event()

            class RecordingCoordinator:
                async def dispatch_ready(self, claim, dispatcher, concurrency):
                    launched.set()
                    return ("run-started",)

            async with sessions.begin() as session:
                active = await session.get(DesktopRun, active_run_id, with_for_update=True)
                active.status = "success"
                active.settled_at = datetime.now(UTC)
            ready_pool = ContextRunPool(
                sessions,
                RecordingCoordinator(),
                object(),
                concurrency=baseline_active + 1,
            )
            assert await ready_pool.drain() == 1
            await asyncio.wait_for(launched.wait(), timeout=1)
            await ready_pool.close()
            async with sessions.begin() as session:
                session.add(DesktopRun(run_id=uuid.uuid4().hex, task_id=context_id, agent_id=f"worker:{context_id}", kind="teammate", status="running", origin="delegated_patrol", execution_thread_id=f"pause-{uuid.uuid4().hex}", loop_id=snapshot["loop_id"], round_id=round_id))
            await service.control(snapshot["loop_id"], "pause")
            async with sessions() as session:
                directive = await session.get(LoopDirective, result.directive_ids[0])
                round_row = await session.get(LoopRound, round_id)
                worker = await session.scalar(select(LoopWorkerRequest).where(LoopWorkerRequest.round_id == round_id))
                run_row = await session.scalar(select(DesktopRun).where(DesktopRun.round_id == round_id, DesktopRun.status == "interrupted"))
                assert directive.status == "cancelled"
                assert worker.status == "cancelled"
                assert round_row.status == "superseded"
                assert run_row is not None
            assert await pool.drain() == 0
            await pool.close()
        finally:
            loop = await service.get(snapshot["loop_id"])
            if loop["status"] in {"running", "paused", "waiting_user"}:
                await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


def test_curator_retry_identity_is_bounded(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, _ = await _create_loop(sessions, tmp_path)
        request_id = uuid.uuid4().hex
        try:
            async with sessions.begin() as session:
                session.add(LoopWorkerRequest(worker_request_id=request_id, loop_id=snapshot["loop_id"], round_id=snapshot["current_round_id"], kind="lane_curator", scope={}, status="running", attempt=1, max_attempts=2, retry_identity=f"worker:{request_id}:attempt:1"))
            runtime = LoopWorkerRuntime(sessions, object(), concurrency=1)
            async with sessions() as session:
                request = await session.get(LoopWorkerRequest, request_id)
            await runtime._fail(request, RuntimeError("transient"))
            async with sessions.begin() as session:
                request = await session.get(LoopWorkerRequest, request_id, with_for_update=True)
                assert request.status == "pending"
                assert request.attempt == 2
                assert request.retry_identity.endswith(":attempt:2")
                request.status = "running"
            async with sessions() as session:
                request = await session.get(LoopWorkerRequest, request_id)
            await runtime._fail(request, RuntimeError("still failing"))
            async with sessions() as session:
                request = await session.get(LoopWorkerRequest, request_id)
                loop = await session.get(AgentLoop, snapshot["loop_id"])
                assert request.status == "error"
                assert request.attempt == 2
                assert loop.status == "waiting_user"
        finally:
            loop = await service.get(snapshot["loop_id"])
            if loop["status"] in {"running", "paused", "waiting_user"}:
                await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


def test_curator_pool_has_its_own_bounded_capacity(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, _ = await _create_loop(sessions, tmp_path)
        release = asyncio.Event()
        completed = asyncio.Event()
        started: list[str] = []
        finished: list[str] = []
        try:
            request_ids = (uuid.uuid4().hex, uuid.uuid4().hex)
            async with sessions.begin() as session:
                for request_id in request_ids:
                    session.add(LoopWorkerRequest(worker_request_id=request_id, loop_id=snapshot["loop_id"], round_id=snapshot["current_round_id"], kind="lane_curator", scope={}, status="pending"))
            runtime = LoopWorkerRuntime(sessions, object(), concurrency=1)

            async def hold(request):
                started.append(request.worker_request_id)
                await release.wait()
                async with sessions.begin() as session:
                    row = await session.get(LoopWorkerRequest, request.worker_request_id, with_for_update=True)
                    row.status = "success"
                    row.completed_at = datetime.now(UTC)
                finished.append(request.worker_request_id)
                if len(finished) == len(request_ids):
                    completed.set()

            runtime._run_one = hold
            assert await runtime.drain() == 1
            while not started:
                await asyncio.sleep(0)
            assert await runtime.drain() == 0
            async with sessions() as session:
                statuses = list((await session.scalars(select(LoopWorkerRequest.status).where(LoopWorkerRequest.worker_request_id.in_(request_ids)).order_by(LoopWorkerRequest.worker_request_id))).all())
                assert statuses == ["pending", "running"] or statuses == ["running", "pending"]
            release.set()
            claimed = 0
            for _ in range(50):
                claimed = await runtime.drain()
                if claimed:
                    break
                await asyncio.sleep(0.01)
            assert claimed == 1
            await asyncio.wait_for(completed.wait(), timeout=5)
            await runtime.close()
            async with sessions() as session:
                statuses = set((await session.scalars(select(LoopWorkerRequest.status).where(LoopWorkerRequest.worker_request_id.in_(request_ids)))).all())
                assert statuses == {"success"}
        finally:
            loop = await service.get(snapshot["loop_id"])
            if loop["status"] in {"running", "paused", "waiting_user"}:
                await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


async def _create_loop(sessions, tmp_path):
    suffix = uuid.uuid4().hex[:8]
    workspace_id = f"ws-pool-{suffix}"
    context_id = f"context-pool-{suffix}"
    revision_id = uuid.uuid4().hex
    loop_id = uuid.uuid4().hex
    workspace_path = tmp_path / workspace_id
    workspace_path.mkdir()
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name="pool"))
        await session.flush()
        session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{suffix}", title="pool"))
        await session.flush()
        ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=f"thread-{suffix}", checkpoint_ns="", checkpoint_id="checkpoint-1", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
        repository = ContextRevisionRepository()
        await repository.insert(session, ContextRevisionContract(ref=ref, content_hash="d" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.ROOT, created_at=datetime.now(UTC)))
        await repository.switch_current(session, ref, None)
        session.add(DesktopRun(run_id=f"initial-{suffix}", task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{suffix}", context_revision_id=revision_id, settled_at=datetime.now(UTC)))
    service = AgentLoopService(sessions)
    snapshot = await service.start(LoopCreateRequest(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, initial_run_id=f"initial-{suffix}", holder_id="patrol-pool", goal="Exercise pools", task_contract="Stay bounded", acceptance_criteria=({"criterion_id": "done", "text": "work settled"},), capabilities=("continue_context", "request_lane_curator", "request_completion"), context_scope=(context_id,), permission_scope=("read", "write")))
    return service, snapshot, context_id, revision_id


async def _frontier(sessions, round_id: str) -> str:
    async with sessions() as session:
        return (await session.get(LoopRound, round_id)).frontier_hash
