"""本文件对外提供 Round 失败收口、延迟 publication 回调与正式暂停的真实 PostgreSQL 竞态回归。

输入为隔离数据库、正式 coordinator claim、预算判定、Patrol session 和 Kernel 已授权的 publication；
输出为统一首锁、无数据库死锁以及暂停后决策与 Round 终态不被迟到失败改写的断言。
具体工作流为在独立事务用原数据库方法取得首锁后控制暂停交错，覆盖预算、派生阻断、legacy 决策和 publication 失败；
另让暂停先完成或在归属读取后完成，确认锁定后的 identity map 刷新及终态保护。legacy 数据仅为明确测试夹具。
示例：pytest backend/tests/test_loop_termination_lock_order.py；不替代原生模型验收。
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.budgets import LoopBudgetGuard
from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionBlocker
from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDecision, LoopRound
from backend.app.desktop.agent_loop.round_orchestration import LoopRoundOrchestrator
from backend.tests.config_helpers import app_config_for
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_context_expansion_kernel import _create_lane_intent, _grant_create_lane_and_add_active_run


pytestmark = pytest.mark.usefixtures('runtime_postgres_database')


@asynccontextmanager
async def _loop_case(tmp_path):
    engine = create_async_engine(os.environ['FOCUS_DATABASE_URL'])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixture = None
    try:
        fixture = await _seed_loop(sessions, tmp_path, label='term-lock', started_at=datetime.now(UTC),
            budgets={'max_model_calls': 1})
        yield sessions, fixture
    finally:
        try:
            if fixture is not None:
                await _stop(fixture['service'], fixture['loop_id'])
        finally:
            await engine.dispose()


async def _queue_publication(sessions, fixture):
    await _grant_create_lane_and_add_active_run(sessions, fixture)
    intent = await _create_lane_intent(sessions, fixture, 'read_only')
    queued = await LoopKernel(sessions, queue_portfolio_publication=True).commit(intent)
    assert queued.status == 'publishing'
    return intent


async def _termination(sessions, fixture, entry):
    claim = await LoopCoordinator(sessions).claim_for_loop(fixture['loop_id'], 'termination-lock-test')
    assert claim is not None
    orchestrator = LoopRoundOrchestrator(sessions, app_config_for('test-model', None), LoopKernel(sessions), None)
    patrol = await orchestrator._patrol_sessions.begin(claim)
    if entry == 'budget':
        exhausted = LoopBudgetGuard().evaluate({'model_calls': 1}, {'max_model_calls': 1}, 'continue_context')
        assert exhausted.status == 'exhausted'
        return partial(orchestrator._budget_exhausted, claim, exhausted.reasons)
    if entry == 'expansion':
        return partial(orchestrator._settle_expansion_blocker, claim, patrol.session_id,
            ExpansionBlocker(code='evidence_insufficient', summary='isolated missing evidence fixture'))
    if entry == 'legacy_decided':
        async with sessions.begin() as session:
            session.add(LoopDecision(decision_id=uuid.uuid4().hex, loop_id=claim.loop_id,
                round_id=claim.round_id, holder_id=fixture['snapshot']['holder_id'], intent={},
                rationale='legacy rejected fixture', status='rejected',
                idempotency_key=f'legacy:{claim.round_id}', rejection={'reason': 'legacy fixture'}))
        return partial(orchestrator.process, claim)
    assert entry == 'deferred_publication'
    intent = await _queue_publication(sessions, fixture)
    return partial(LoopKernel(sessions)._fail_deferred, intent.decision_id, 'isolated publication failure')


async def _race_pause(terminate, fixture, monkeypatch):
    start_control, control_locked = asyncio.Event(), asyncio.Event()
    original_get, original_scalar = AsyncSession.get, AsyncSession.scalar
    first_lock = []

    async def interleave(session, entity, ident, **kwargs):
        value = await original_get(session, entity, ident, **kwargs)
        if (asyncio.current_task().get_name() == 'termination' and kwargs.get('with_for_update')
            and entity in {AgentLoop, LoopRound, LoopDecision} and not first_lock):
            first_lock.append(entity)
            start_control.set()
            if entity is not AgentLoop:
                await asyncio.wait_for(control_locked.wait(), 5)
        return value

    async def control_scalar(session, statement, **kwargs):
        value = await original_scalar(session, statement, **kwargs)
        if asyncio.current_task().get_name() == 'pause-control' and isinstance(value, AgentLoop):
            control_locked.set()
        return value

    monkeypatch.setattr(AsyncSession, 'get', interleave)
    monkeypatch.setattr(AsyncSession, 'scalar', control_scalar)

    async def pause():
        await asyncio.wait_for(start_control.wait(), 5)
        try:
            return await fixture['service'].control(fixture['loop_id'], 'pause')
        except HTTPException as exc:
            assert exc.status_code == 409
            return {'state_conflict': True}

    results = await asyncio.wait_for(asyncio.gather(
        asyncio.create_task(terminate(), name='termination'),
        asyncio.create_task(pause(), name='pause-control'), return_exceptions=True), 15)
    errors = [result for result in results if isinstance(result, BaseException)]
    assert not errors, [type(error).__name__ for error in errors]
    assert first_lock == [AgentLoop]


@pytest.mark.parametrize('entry', ('budget', 'expansion', 'legacy_decided', 'deferred_publication'))
def test_termination_and_pause_share_first_loop_lock(tmp_path, monkeypatch, entry):
    async def run():
        async with _loop_case(tmp_path) as (sessions, fixture):
            terminate = await _termination(sessions, fixture, entry)
            await _race_pause(terminate, fixture, monkeypatch)
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                current = await session.get(LoopRound, fixture['round_id'])
                assert loop.status in {'paused', 'waiting_user'}
                assert current.status in {'superseded', 'error'}
                assert loop.current_round_id == current.round_id
    asyncio.run(run())


def test_late_deferred_failure_preserves_pause_terminal(tmp_path):
    async def run():
        async with _loop_case(tmp_path) as (sessions, fixture):
            intent = await _queue_publication(sessions, fixture)
            await fixture['service'].control(fixture['loop_id'], 'pause')
            async with sessions() as session:
                decision = await session.get(LoopDecision, intent.decision_id)
                assert decision.status == 'superseded'
                rejection = dict(decision.rejection)
            result = await LoopKernel(sessions)._fail_deferred(intent.decision_id, 'late isolated failure')
            assert result.status == 'superseded'
            async with sessions() as session:
                decision = await session.get(LoopDecision, intent.decision_id)
                loop = await session.get(AgentLoop, fixture['loop_id'])
                current = await session.get(LoopRound, fixture['round_id'])
                assert decision.status == 'superseded'
                assert decision.rejection == rejection
                assert loop.status == 'paused'
                assert current.status == 'superseded'
    asyncio.run(run())


def test_deferred_failure_refreshes_pause_after_identity_read(tmp_path, monkeypatch):
    async def run():
        async with _loop_case(tmp_path) as (sessions, fixture):
            intent = await _queue_publication(sessions, fixture)
            original_get = AsyncSession.get
            observed_status = []

            async def interleave(session, entity, ident, **kwargs):
                value = await original_get(session, entity, ident, **kwargs)
                if (asyncio.current_task().get_name() == 'deferred-failure' and entity is LoopDecision
                    and ident == intent.decision_id and not kwargs.get('with_for_update') and not observed_status):
                    observed_status.append(value.status)
                    await fixture['service'].control(fixture['loop_id'], 'pause')
                return value

            monkeypatch.setattr(AsyncSession, 'get', interleave)
            result = await asyncio.create_task(
                LoopKernel(sessions)._fail_deferred(intent.decision_id, 'failure after identity read'),
                name='deferred-failure')
            assert observed_status == ['publishing']
            assert result.status == 'superseded'
            async with sessions() as session:
                decision = await session.get(LoopDecision, intent.decision_id)
                loop = await session.get(AgentLoop, fixture['loop_id'])
                assert decision.status == 'superseded'
                assert decision.rejection == {'reason': 'loop_paused'}
                assert loop.status == 'paused'
    asyncio.run(run())
