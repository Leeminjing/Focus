r"""本文件对外提供发布队列、Context Run 排队、Curator 重试与 pause 收敛回归。

输入为真实 PostgreSQL Loop、阻塞发布、耗尽的 Context 容量、失败 Worker 和 pause 控制；输出为独立进度、
持久 queued_reason、有界 attempt identity 及全部活动工作终态断言。具体工作流为创建最小 Loop 后逐一驱动
三个独立运行路径，以提交后的完成事件同步 Worker，重试须重新领取唯一身份，再从持久实体读取结果。示例：`pytest backend/tests/test_loop_runtime_pools.py`。
波次夹具使用真实 Context、Lane 和 Kernel 授权；事件屏障验证批量容量预留与 running Round 补位，不以轮询延迟假装并行。
多 Loop 竞争与失败重放检查按 Run 预留的全局上限及独立 AsyncSession，不共享事务会话。
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
    LoopContextMembership,
    LoopDelegationGrant,
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
from backend.app.desktop.context_curation.models import CurationLane
from backend.app.desktop.agent_loop.rounds import current_frontier_hash

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
            assert await pool.drain(snapshot["loop_id"]) == 0
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
            assert await ready_pool.drain(snapshot["loop_id"]) == 1
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
            assert await pool.drain(snapshot["loop_id"]) == 0
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
                assert request.retry_identity is None
            request = (await runtime._claim_many(1, snapshot["loop_id"]))[0]
            assert ":attempt:2:" in request.retry_identity
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


async def _create_loop(sessions, tmp_path, *, git=False):
    suffix = uuid.uuid4().hex[:8]
    workspace_id = f"ws-pool-{suffix}"
    context_id = f"context-pool-{suffix}"
    revision_id = uuid.uuid4().hex
    loop_id = uuid.uuid4().hex
    workspace_path = tmp_path / workspace_id
    workspace_path.mkdir()
    if git:
        from backend.tests.test_git_workspace_adoption import _repository

        _repository(workspace_path)
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


async def _create_wave(sessions, tmp_path, *, count=3, writing=False, git=False, action_count=None):
    service, snapshot, primary_id, primary_revision = await _create_loop(sessions, tmp_path, git=git)
    loop_id, round_id = snapshot["loop_id"], snapshot["current_round_id"]
    contexts = [(primary_id, primary_revision)]
    repository = ContextRevisionRepository()
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, loop_id)
        loop.equipment = {"permissions": ["read", "write"] if writing else ["read"]}
        for index in range(1, count):
            context_id, revision_id, lane_id = (uuid.uuid4().hex for _ in range(3))
            session.add(DesktopThread(task_id=context_id, workspace_id=loop.workspace_id,
                                      thread_id=f"thread-{context_id}", title=f"module-{index}"))
            await session.flush()
            ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1,
                execution_thread_id=f"thread-{context_id}", checkpoint_ns="", checkpoint_id="fixture-checkpoint",
                payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
            await repository.insert(session, ContextRevisionContract(ref=ref, content_hash="d" * 64,
                projection_status=ContextRevisionProjectionStatus.VALID,
                origin_kind=ContextRevisionOriginKind.ROOT, created_at=datetime.now(UTC)))
            await repository.switch_current(session, ref, None)
            session.add(CurationLane(lane_id=lane_id, program_id=loop.program_id,
                managed_context_id=context_id, purpose=f"module-{index}", normalized_purpose=f"module-{index}",
                lane_policy={"workspace_mode": "isolated_write" if writing else "read_only"}))
            await session.flush()
            session.add(LoopContextMembership(membership_id=uuid.uuid4().hex, loop_id=loop_id,
                context_id=context_id, lane_id=lane_id, role="side", status="active"))
            contexts.append((context_id, revision_id))
        grant = await session.get(LoopDelegationGrant, snapshot["grant"]["grant_id"])
        grant.context_scope = [context_id for context_id, _ in contexts]
        if writing:
            grant.capabilities = [*grant.capabilities, "isolate_workspace", "adopt_workspace_result"]
        await session.flush()
        round_row = await session.get(LoopRound, round_id)
        round_row.frontier_hash = await current_frontier_hash(session, loop_id)
    intent = PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key=f"wave:{round_id}",
        loop_id=loop_id, loop_revision=snapshot["revision"], round_id=round_id,
        holder_id=snapshot["holder_id"], grant_id=snapshot["grant"]["grant_id"], grant_revision=1,
        goal_revision=1, observed_frontier_hash=await _frontier(sessions, round_id),
        observed_workspace_revision=1, rationale="Independent modules share a verified input contract.",
        actions=tuple({"action": "continue_context", "context_id": context_id,
            "context_revision_id": revision_id, "message": f"Implement module {index}."}
            for index, (context_id, revision_id) in enumerate(contexts[:action_count])))
    result = await LoopKernel(sessions).commit(intent)
    assert result.status == "committed", result
    return service, snapshot, contexts, result.directive_ids


@pytest.mark.parametrize("round_status", ["ready", "running"])
@pytest.mark.parametrize("global_limit,loop_limit,expected", [(1, 2, 1), (2, 1, 1), (2, 2, 2)])
def test_context_pool_reserves_a_wave_and_refills_running_round(tmp_path, round_status, global_limit, loop_limit, expected):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, _, _ = await _create_wave(sessions, tmp_path)
        release, started = asyncio.Event(), asyncio.Event()
        capacities = []

        class Coordinator:
            async def dispatch_ready(self, claim, dispatcher, concurrency):
                capacities.append(concurrency)
                started.set()
                await release.wait()
                return ()

        pool = ContextRunPool(sessions, Coordinator(), object(), concurrency=global_limit)
        try:
            async with sessions.begin() as session:
                grant = await session.get(LoopDelegationGrant, snapshot["grant"]["grant_id"])
                grant.budgets = {**grant.budgets, "max_concurrent_runs": loop_limit}
                row = await session.get(LoopRound, snapshot["current_round_id"])
                row.status = round_status
            assert await pool.drain(snapshot["loop_id"]) == 1
            await asyncio.wait_for(started.wait(), 2)
            assert capacities == [expected]
            assert await pool.drain(snapshot["loop_id"]) == 0
        finally:
            release.set()
            await pool.close()
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


def test_multiple_loops_compete_with_run_reservations_and_failed_launch_releases_capacity(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        first, a, _, _ = await _create_wave(sessions, tmp_path)
        second, b, _, _ = await _create_wave(sessions, tmp_path)
        release = asyncio.Event()
        started = asyncio.Event()
        calls = []
        async with sessions.begin() as session:
            for snapshot in (a, b):
                grant = await session.get(LoopDelegationGrant, snapshot["grant"]["grant_id"])
                grant.budgets = {**grant.budgets, "max_concurrent_runs": 1}
        class Coordinator:
            async def dispatch_ready(self, claim, dispatcher, concurrency):
                calls.append((claim.loop_id, concurrency))
                if len(calls) == 2:
                    started.set()
                await release.wait()
                if claim.loop_id == a["loop_id"]:
                    raise RuntimeError("test launch failure")
                return ()
        pool = ContextRunPool(sessions, Coordinator(), object(), concurrency=2)
        try:
            assert await asyncio.gather(pool.drain(), pool.drain()) == [2, 0]
            await asyncio.wait_for(started.wait(), 5)
            assert sorted(calls) == sorted([(a["loop_id"], 1), (b["loop_id"], 1)])
            release.set()
            await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            assert await pool.drain() == 2
        finally:
            release.set()
            await pool.close()
            await first.control(a["loop_id"], "stop")
            await second.control(b["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())


def test_loop_at_its_limit_does_not_hide_another_loop_with_global_capacity(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        first, a, contexts, _ = await _create_wave(sessions, tmp_path)
        second, b, _, _ = await _create_wave(sessions, tmp_path)
        async with sessions.begin() as session:
            grant = await session.get(LoopDelegationGrant, a["grant"]["grant_id"])
            grant.budgets = {**grant.budgets, "max_concurrent_runs": 1}
            session.add(DesktopRun(run_id=uuid.uuid4().hex, task_id=contexts[0][0], agent_id="capacity-worker",
                kind="teammate", status="running", origin="delegated_patrol", execution_thread_id=uuid.uuid4().hex,
                loop_id=a["loop_id"], round_id=a["current_round_id"]))
        calls = []
        class Coordinator:
            async def dispatch_ready(self, claim, dispatcher, concurrency):
                calls.append((claim.loop_id, concurrency))
                return ()
        pool = ContextRunPool(sessions, Coordinator(), object(), concurrency=2)
        try:
            assert await pool.drain() == 1
            await asyncio.gather(*(task for _, _, task in pool._tasks.values()))
            assert calls == [(b["loop_id"], 1)]
        finally:
            await pool.close()
            await first.control(a["loop_id"], "stop")
            await second.control(b["loop_id"], "stop")
            await engine.dispose()
    asyncio.run(run())
