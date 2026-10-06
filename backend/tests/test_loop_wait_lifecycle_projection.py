"""本文件对外提供 waiting_user 状态的真实 journal/reducer 回归。

输入为隔离 PostgreSQL 中的实际 Loop、等待、执行阶段及用户响应；输出为实时 Loop 生命周期与持久状态一致且阶段更新不撤销执行授权的断言。
具体工作流为通过等待服务进入/离开 waiting_user，改变执行阶段，暂停恢复到尚未冻结的新轮并改变其屏障，再只归约规范事件，验证无需数据库 overlay 或前端猜测；示例：pytest backend/tests/test_loop_wait_lifecycle_projection.py。
"""

import asyncio
from datetime import UTC, datetime
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from backend.app.desktop.agent_loop.live_projection_projector import LoopLiveSnapshotProjector
from backend.app.desktop.agent_loop.wait_requests import LoopWaitRequestFactory, LoopWaitRequestService
from backend.app.desktop.agent_loop.models import AgentLoop, LoopRound
from backend.app.desktop.agent_loop.schemas import LoopWaitResponseRequest
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_resume_publishes_unfrozen_round_and_each_changed_barrier_once(tmp_path):
    async def run():
        from sqlalchemy import func, select
        from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
        from backend.app.desktop.agent_loop.lifecycle_events import LoopLifecycleEventRecorder

        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="resume-unfrozen", started_at=datetime.now(UTC))
        try:
            await fixture["service"].control(fixture["loop_id"], "pause")
            async with sessions() as session:
                before = await LoopLiveSnapshotProjector().project(session, fixture["loop_id"])
            await fixture["service"].control(fixture["loop_id"], "resume")
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                row = await session.get(LoopRound, loop.current_round_id)
                resumed = await LoopLiveSnapshotProjector().project(session, fixture["loop_id"], base=before)
                assert row.observation_id is None and row.number == 2
                assert resumed.round.entity_id == row.round_id
                assert resumed.round.state["number"] == 2
                assert resumed.patrol_session is None
                round_id = row.round_id
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                row = await session.get(LoopRound, round_id)
                loop.health = "progress_waiting"
                row.barrier = {"pending": ["run-1"]}
                await LoopLifecycleEventRecorder().record(session, loop)
                row.barrier = {"pending": ["run-2"]}
                await LoopLifecycleEventRecorder().record(session, loop)
                await LoopLifecycleEventRecorder().record(session, loop)
            async with sessions() as session:
                current = await LoopLiveSnapshotProjector().project(session, fixture["loop_id"], base=resumed)
                assert current.round.state["barrier"] == {"pending": ["run-2"]}
                assert current.round.revision == resumed.round.revision + 2
                assert await session.scalar(select(func.count()).select_from(LoopJournalEvent).where(
                    LoopJournalEvent.loop_id == fixture["loop_id"], LoopJournalEvent.entity_type == "round",
                    LoopJournalEvent.entity_id == round_id)) == 3
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_runtime_stage_versions_do_not_advance_control_or_hide_later_pause(tmp_path):
    async def run():
        from backend.app.desktop.agent_loop.lifecycle_events import LoopLifecycleEventRecorder
        from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy
        from backend.app.desktop.models import DesktopRun
        from test_loop_execution_ownership import _seed_launching_directive, _admit_run

        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="stage-live", started_at=datetime.now(UTC))
        try:
            directive_id = await _seed_launching_directive(sessions, fixture, label="stage-live")
            run_id = await _admit_run(sessions, fixture, directive_id)
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                control_revision = loop.revision
                for health in ("curating", "deciding", "waiting_runs", "dispatching", "waiting_runs"):
                    loop.health = health
                    await LoopLifecycleEventRecorder().record(session, loop)
                    await RunOwnershipPolicy().assert_live(session, await session.get(DesktopRun, run_id))
                assert loop.revision == control_revision
            async with sessions() as session:
                before = await LoopLiveSnapshotProjector().project(session, fixture["loop_id"])
                assert before.loop.state["health"] == "waiting_runs"
                assert before.loop.state["revision"] == control_revision
                assert before.loop.revision > control_revision
            await fixture["service"].control(fixture["loop_id"], "pause")
            async with sessions() as session:
                paused = await LoopLiveSnapshotProjector().project(session, fixture["loop_id"], base=before)
                assert paused.loop.state["status"] == "paused"
                assert paused.loop.state["revision"] == control_revision + 1
                assert paused.loop.revision > before.loop.revision
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


def test_wait_open_and_resolve_publish_monotonic_loop_lifecycle(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="wait-live", started_at=datetime.now(UTC))
            service = LoopWaitRequestService()
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                request = await service.open(session, loop, LoopWaitRequestFactory.clarification("选择测试范围"),
                    created_by="test", correlation_id="wait-live")
                request_id, revision = request.request_id, request.revision
            async with sessions() as session:
                waiting = await LoopLiveSnapshotProjector().project(session, fixture["loop_id"])
                assert waiting.loop.state["status"] == "waiting_user"
                assert waiting.loop.state["waiting_reason"] == "选择测试范围"
            async with sessions.begin() as session:
                await service.resolve(session, request_id, answer={"text": "测试 fixture"}, actor_id="user",
                    request_revision=revision, idempotency_key=f"answer:{request_id}")
            async with sessions() as session:
                resumed = await LoopLiveSnapshotProjector().project(session, fixture["loop_id"], base=waiting)
                assert resumed.loop.state["status"] == "running"
                assert resumed.loop.state["waiting_reason"] is None
                assert resumed.loop.revision > waiting.loop.revision
        finally:
            if fixture:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_user_wait_response_publishes_the_successor_round_at_a_new_control_revision(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="wait-pointer", started_at=datetime.now(UTC))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                (await session.get(LoopRound, loop.current_round_id)).status = "error"
                request = await LoopWaitRequestService().open(session, loop,
                    LoopWaitRequestFactory.retry_or_stop("重试失败轮"), created_by="test", correlation_id="wait-pointer")
                request_id = request.request_id
            body = LoopWaitResponseRequest(request_revision=1, idempotency_key=f"retry:{request_id}", answer={"action": "retry"})
            first = await fixture["service"].resolve_wait_request(fixture["loop_id"], request_id, body, actor_id="user")
            repeated = await fixture["service"].resolve_wait_request(fixture["loop_id"], request_id, body, actor_id="user")
            assert first["created"] and not repeated["created"]
            async with sessions() as session:
                stored = await session.get(AgentLoop, fixture["loop_id"])
                projected = await LoopLiveSnapshotProjector().project(session, fixture["loop_id"])
                assert stored.current_round_id != fixture["round_id"]
                assert projected.loop.state["current_round_id"] == stored.current_round_id
                assert projected.loop.state["health"] == stored.health == "observing"
                assert projected.loop.state["revision"] == stored.revision
                assert projected.round.entity_id == stored.current_round_id
        finally:
            if fixture:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
