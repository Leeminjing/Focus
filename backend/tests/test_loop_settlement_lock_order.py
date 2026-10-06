"""本文件对外提供 Run 结算与 Worker 推进的真实行锁竞态回归。

输入为隔离 PostgreSQL、正式 Kernel 和实际完成 Worker；输出为两个回调均完成、统计一次且只创建一个后继轮的断言。
具体工作流为保存当前轮真实 Worker 结果和已结算 Run，在不同事务同时调用生产结算及推进入口；另以过期观察轮竞争看门狗与正式暂停控制。
调度屏障只控制首锁交错，不替代数据库读写；控制竞争只接受正常终态或明确状态冲突，不接受数据库死锁。
示例：pytest backend/tests/test_loop_settlement_lock_order.py。
"""

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopRound
from backend.app.desktop.models import DesktopRun
from backend.tests.test_loop_round_progress_admission import _prepare, _intent
from backend.tests.test_loop_worker_recovery import _case, _step
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.app.desktop.agent_loop.rounds import RoundStallLimits, terminate_stalled_rounds


pytestmark = pytest.mark.usefixtures('runtime_postgres_database')


def test_run_settlement_and_worker_advance_share_first_loop_lock(tmp_path, monkeypatch):
    async def run():
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            await _prepare(sessions, fixture, identity)
            assert (await LoopKernel(sessions).commit(_intent(observation))).status == 'committed'
            original_advance = runtime._advance

            async def hold(*args, **kwargs):
                return None

            monkeypatch.setattr(runtime, '_advance', hold)
            provider.valid = True
            await _step(runtime, fixture['loop_id'])
            run_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                current = await session.get(LoopRound, observation.round_id)
                current.status = 'running'
                session.add(DesktopRun(run_id=run_id, task_id=fixture['context_id'],
                    agent_id=uuid.uuid4().hex, kind='worker', status='success', origin='delegated_patrol',
                    loop_id=fixture['loop_id'], round_id=current.round_id,
                    workspace_result={'revision': current.workspace_revision, 'workspace_status': 'settled'},
                    settled_at=datetime.now(UTC)))
            start_worker, worker_loop_locked = asyncio.Event(), asyncio.Event()
            original_get = AsyncSession.get
            first_lock = []

            async def interleave(session, entity, ident, **kwargs):
                value = await original_get(session, entity, ident, **kwargs)
                owner = asyncio.current_task().get_name()
                if kwargs.get('with_for_update'):
                    if owner == 'run-settler' and entity in {AgentLoop, LoopRound} and not first_lock:
                        first_lock.append(entity)
                        start_worker.set()
                        if entity is LoopRound:
                            await asyncio.wait_for(worker_loop_locked.wait(), 5)
                    elif owner == 'worker-advance' and entity is AgentLoop:
                        worker_loop_locked.set()
                return value

            monkeypatch.setattr(AsyncSession, 'get', interleave)

            async def settle():
                async with sessions.begin() as session:
                    await LoopCoordinator(sessions).handle_run_settled(
                        SimpleNamespace(run_id=run_id, event_id=uuid.uuid4().hex, payload={}), session)

            async def advance():
                await asyncio.wait_for(start_worker.wait(), 5)
                await original_advance(fixture['loop_id'], observation.round_id)

            results = await asyncio.wait_for(asyncio.gather(
                asyncio.create_task(settle(), name='run-settler'),
                asyncio.create_task(advance(), name='worker-advance'), return_exceptions=True), 15)
            assert not [r for r in results if isinstance(r, BaseException)], results
            assert first_lock == [AgentLoop]
            async with sessions() as session:
                usage = await session.get(LoopBudgetUsage, fixture['loop_id'])
                assert usage.rounds == usage.no_progress_count == 1
                assert await session.scalar(select(func.count()).select_from(LoopRound).where(
                    LoopRound.loop_id == fixture['loop_id'])) == 2

    asyncio.run(run())


def test_watchdog_and_pause_control_share_first_loop_lock(tmp_path, monkeypatch):
    async def run():
        engine = create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label='watchdog-lock', started_at=datetime.now(UTC))
        start_control, control_locked = asyncio.Event(), asyncio.Event()
        original_get, original_scalar = AsyncSession.get, AsyncSession.scalar
        first_lock = []

        async def interleave(session, entity, ident, **kwargs):
            value = await original_get(session, entity, ident, **kwargs)
            if (asyncio.current_task().get_name() == 'watchdog' and kwargs.get('with_for_update')
                and entity in {AgentLoop, LoopRound} and not first_lock):
                first_lock.append(entity)
                start_control.set()
                if entity is LoopRound:
                    await asyncio.wait_for(control_locked.wait(), 5)
            return value

        async def control_scalar(session, statement, **kwargs):
            value = await original_scalar(session, statement, **kwargs)
            if asyncio.current_task().get_name() == 'pause-control' and isinstance(value, AgentLoop):
                control_locked.set()
            return value

        monkeypatch.setattr(AsyncSession, 'get', interleave)
        monkeypatch.setattr(AsyncSession, 'scalar', control_scalar)

        async def watchdog():
            async with sessions.begin() as session:
                return await terminate_stalled_rounds(session, RoundStallLimits(max_round_seconds=0),
                    datetime.now(UTC), category='watchdog')

        async def pause():
            await asyncio.wait_for(start_control.wait(), 5)
            try:
                return await fixture['service'].control(fixture['loop_id'], 'pause')
            except HTTPException as exc:
                assert exc.status_code == 409
                return {'state_conflict': True}

        try:
            results = await asyncio.wait_for(asyncio.gather(
                asyncio.create_task(watchdog(), name='watchdog'),
                asyncio.create_task(pause(), name='pause-control'), return_exceptions=True), 15)
            assert not [r for r in results if isinstance(r, BaseException)], results
            assert first_lock == [AgentLoop]
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                current = await session.get(LoopRound, fixture['round_id'])
                assert loop.status in {'paused', 'waiting_user'}
                assert current.status in {'superseded', 'error'}
                assert loop.current_round_id == current.round_id
                assert await session.scalar(select(func.count()).select_from(LoopRound).where(
                    LoopRound.loop_id == loop.loop_id)) == 1
        finally:
            await _stop(fixture['service'], fixture['loop_id'])
            await engine.dispose()
    asyncio.run(run())
