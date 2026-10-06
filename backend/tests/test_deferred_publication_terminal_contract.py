"""本文件对外提供被取代的延迟发布在真实PostgreSQL中的终止与暂停竞态回归。
输入为生产Kernel授权的发布、队列认领与受控PortfolioSuperseded；输出为一次终结事件、结算时间、准确等待及暂停fence断言。
具体工作流为从合法发布意图进入两种失败入口，复验幂等回调与真实首锁交错；不调用Provider或修改用户数据。
示例：pytest backend/tests/test_deferred_publication_terminal_contract.py -q；原模型非法Lane purpose拒绝仍保留。
"""

import asyncio
from functools import partial

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDecision, LoopEventOutbox, LoopRound
from backend.app.desktop.agent_loop.publication_queue import LoopPortfolioPublicationQueue
from backend.app.desktop.context_curation.portfolio_publisher import PortfolioSuperseded
from backend.tests.test_loop_termination_lock_order import _loop_case, _queue_publication, _race_pause


pytestmark = pytest.mark.usefixtures('runtime_postgres_database')


class _SupersededPublisher:
    async def publish(self, decision_id):
        raise PortfolioSuperseded('Controlled stale publication contract')


async def _assert_terminal(sessions, fixture, intent):
    async with sessions() as session:
        loop = await session.get(AgentLoop, fixture['loop_id'])
        current = await session.get(LoopRound, fixture['round_id'])
        decision = await session.get(LoopDecision, intent.decision_id)
        assert decision.status == current.status == 'superseded'
        assert current.settled_at is not None
        assert loop.status == 'waiting_user' and loop.health == 'degraded'
        assert 'publication' in loop.waiting_reason.lower() or '发布' in loop.waiting_reason
        count = await session.scalar(select(func.count()).select_from(LoopEventOutbox).where(
            LoopEventOutbox.loop_id == fixture['loop_id'], LoopEventOutbox.event_type == 'RoundTerminated'))
        assert count == 1
        assert await session.scalar(select(func.count()).select_from(LoopRound).where(
            LoopRound.loop_id == fixture['loop_id'])) == 1


@pytest.mark.parametrize('entry', ['kernel', 'queue'])
def test_superseded_publication_closes_round_and_returns_current_loop_to_user(tmp_path, entry):
    async def run():
        async with _loop_case(tmp_path) as (sessions, fixture):
            intent = await _queue_publication(sessions, fixture)
            if entry == 'kernel':
                result = await LoopKernel(sessions)._fail_deferred(intent.decision_id, 'Controlled stale publication', superseded=True)
                assert result.status == 'superseded'
                await _assert_terminal(sessions, fixture, intent)
                await LoopKernel(sessions)._fail_deferred(intent.decision_id, 'Duplicate stale publication', superseded=True)
            else:
                queue = LoopPortfolioPublicationQueue(sessions, _SupersededPublisher())
                assert await queue._claim(1, fixture['loop_id']) == (intent.decision_id,)
                await queue._run(intent.decision_id)
                await _assert_terminal(sessions, fixture, intent)
                assert not await queue._settle_failure(intent.decision_id, 'Duplicate stale publication', superseded=True)
            await _assert_terminal(sessions, fixture, intent)
    asyncio.run(run())


def test_queue_failure_and_pause_use_same_first_loop_lock(tmp_path, monkeypatch):
    async def run():
        async with _loop_case(tmp_path) as (sessions, fixture):
            intent = await _queue_publication(sessions, fixture)
            queue = LoopPortfolioPublicationQueue(sessions, _SupersededPublisher())
            assert await queue._claim(1, fixture['loop_id']) == (intent.decision_id,)
            await _race_pause(partial(queue._settle_failure, intent.decision_id, 'Controlled publication failure', superseded=True),
                              fixture, monkeypatch)
    asyncio.run(run())


def test_late_queue_failure_preserves_paused_decision_and_round(tmp_path):
    async def run():
        async with _loop_case(tmp_path) as (sessions, fixture):
            intent = await _queue_publication(sessions, fixture)
            queue = LoopPortfolioPublicationQueue(sessions, _SupersededPublisher())
            assert await queue._claim(1, fixture['loop_id']) == (intent.decision_id,)
            await fixture['service'].control(fixture['loop_id'], 'pause')
            assert not await queue._settle_failure(intent.decision_id, 'Late stale publication', superseded=True)
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                decision = await session.get(LoopDecision, intent.decision_id)
                current = await session.get(LoopRound, fixture['round_id'])
                assert loop.status == 'paused'
                assert decision.status == current.status == 'superseded'
                assert decision.rejection == {'reason': 'loop_paused'}
    asyncio.run(run())


def test_queue_failure_refreshes_pause_after_reading_decision_identity(tmp_path, monkeypatch):
    async def run():
        async with _loop_case(tmp_path) as (sessions, fixture):
            intent = await _queue_publication(sessions, fixture)
            queue = LoopPortfolioPublicationQueue(sessions, _SupersededPublisher())
            assert await queue._claim(1, fixture['loop_id']) == (intent.decision_id,)
            original_get = AsyncSession.get
            observed = []

            async def interleave(session, entity, ident, **kwargs):
                value = await original_get(session, entity, ident, **kwargs)
                if entity is LoopDecision and ident == intent.decision_id and not kwargs.get('with_for_update') and not observed:
                    observed.append(value.status)
                    await fixture['service'].control(fixture['loop_id'], 'pause')
                return value

            monkeypatch.setattr(AsyncSession, 'get', interleave)
            assert not await queue._settle_failure(intent.decision_id, 'Late failure after identity read', superseded=True)
            assert observed == ['publishing_run']
            async with sessions() as session:
                assert (await session.get(AgentLoop, fixture['loop_id'])).status == 'paused'
                assert (await session.get(LoopDecision, intent.decision_id)).rejection == {'reason': 'loop_paused'}
    asyncio.run(run())
