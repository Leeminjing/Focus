"""本文件对外提供Worker结果与已知模型消费同事务收口的生产回归。
输入为隔离PostgreSQL、真实WorkerRuntime及仅替换远端响应的Provider；输出为单次采样、实际或unknown消费与回滚重放断言。
具体工作流为注入独立回执提交失败，核对结果提交补交原receipt且不重复调用模型，再验证事务回滚后幂等重放。
暂停竞争只补消费而不发布结果；重启分类只处理已终结或旧领取身份的unknown，保留预算及迟到实测回填。
示例：pytest backend/tests/test_loop_worker_receipt_commit.py；不伪造历史预留或写真实工作区。
"""

import asyncio
from datetime import UTC, datetime
import json
import os
import uuid

from langchain_core.messages import AIMessage
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.context_expansion.index_budget_repository import IndexBudgetReservationRepository
from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopWorkerRequest
from backend.app.desktop.agent_loop.usage import LoopUsageDelta
from backend.app.desktop.agent_loop.worker_model_usage import WorkerModelUsage
from backend.app.desktop.agent_loop.worker_receipt_recovery import WorkerReceiptRecovery
from backend.tests.config_helpers import app_config_for
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_loop_model_role_receipts import _run_worker

pytestmark = pytest.mark.usefixtures('isolated_postgres_database')


@pytest.mark.parametrize('kind', ['lane_curator', 'completion_verifier'])
@pytest.mark.parametrize('reported', [True, False])
def test_worker_result_commits_original_receipt_without_model_resampling(tmp_path, monkeypatch, kind, reported):
    async def run():
        engine = create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label='receipt-commit', started_at=datetime.now(UTC))
        config = app_config_for('receipt-commit', None)
        config.models[0].curation_output_method = 'prompt_json'
        calls = []
        method_name = 'settle' if reported else 'mark_unknown'

        async def unavailable_independent_commit(self, *args):
            raise TimeoutError('injected independent receipt transaction failure')

        class Provider:
            async def ainvoke(self, messages, config):
                calls.append(messages)
                if reported:
                    config['callbacks'][0].usage_metadata['receipt-commit'] = {'input_tokens': 40, 'output_tokens': 10, 'total_tokens': 50}
                response = {'rationale': '现有Context足够', 'work_specs': []} if kind == 'lane_curator' else {
                    'criteria': [{'check_id': 'tests', 'status': 'unknown', 'explanation': '缺少证据'}], 'conclusion': 'unknown'}
                return AIMessage(content=json.dumps(response))

        monkeypatch.setattr(IndexBudgetReservationRepository, method_name, unavailable_independent_commit)
        monkeypatch.setattr('backend.app.desktop.agent_loop.structured_worker.create_chat_model', lambda **kwargs: Provider())
        try:
            worker_id = await _run_worker(sessions, config, fixture, kind)
            assert len(calls) == 1
            async with sessions() as session:
                rows = tuple((await session.scalars(select(LoopIndexBudgetReservation).where(LoopIndexBudgetReservation.loop_id == fixture['loop_id']))).all())
                assert len(rows) == 1
                receipt = rows[0]
                assert receipt.actual_usage['owner_id'] == worker_id
                usage = await session.get(LoopBudgetUsage, fixture['loop_id'])
                if reported:
                    assert receipt.settled_at is not None
                    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (1, 40, 10)
                else:
                    assert receipt.settled_at is None and receipt.actual_usage['receipt_state'] == 'unknown'
                    assert usage.model_calls == 0
        finally:
            await _stop(fixture['service'], fixture['loop_id'])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('state', ['running', 'success', 'pending', 'cancelled'])
def test_orphan_receipt_recovery_preserves_budget_and_late_actual_report(tmp_path, state):
    async def run():
        engine = create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label='rcpt-orphan', started_at=datetime.now(UTC))
        key = uuid.uuid4().hex
        try:
            async with sessions.begin() as session:
                request = LoopWorkerRequest(worker_request_id=key, loop_id=fixture['loop_id'],
                    round_id=fixture['round_id'], kind='lane_curator', scope={}, status='running', retry_identity='receipt-owner')
                session.add(request)
            receipt = await WorkerModelUsage(sessions, request).reserve(100, 50)
            async with sessions.begin() as session:
                row = await session.get(LoopWorkerRequest, key)
                row.status = state
                if state == 'pending':
                    row.retry_identity = None
            async with sessions.begin() as session:
                assert await WorkerReceiptRecovery().reconcile(session) == int(state != 'running')
            async with sessions.begin() as session:
                assert await WorkerReceiptRecovery().reconcile(session) == 0
            async with sessions() as session:
                stored = await session.get(LoopIndexBudgetReservation, receipt._reservation_id)
                usage = await session.get(LoopBudgetUsage, fixture['loop_id'])
                assert stored.settled_at is None and stored.model_calls == 1 and usage.model_calls == 0
                assert stored.actual_usage.get('receipt_state') == (None if state == 'running' else 'unknown')
            await receipt.settle(LoopUsageDelta(model_calls=1, input_tokens=40, output_tokens=10))
            async with sessions() as session:
                assert (await session.get(LoopWorkerRequest, key)).status == state
                assert (await session.get(AgentLoop, fixture['loop_id'])).current_round_id == fixture['round_id']
                assert (await session.get(LoopBudgetUsage, fixture['loop_id'])).model_calls == 1
        finally:
            await _stop(fixture['service'], fixture['loop_id'])
            await engine.dispose()
    asyncio.run(run())


def test_receipt_result_transaction_rollback_and_replay_are_atomic(tmp_path):
    async def run():
        engine = create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label='rcpt-rollback', started_at=datetime.now(UTC))
        repository = IndexBudgetReservationRepository(sessions, fixture['loop_id'], 1, {}, {})
        delta = LoopUsageDelta(model_calls=1, input_tokens=40, output_tokens=10)
        try:
            await repository.reserve(100, 50)
            with pytest.raises(RuntimeError, match='rollback result'):
                async with sessions.begin() as session:
                    await repository.settle_in(session, delta)
                    raise RuntimeError('rollback result')
            async with sessions() as session:
                receipt = await session.get(LoopIndexBudgetReservation, repository._reservation_id)
                usage = await session.get(LoopBudgetUsage, fixture['loop_id'])
                assert receipt.settled_at is None and usage.model_calls == 0
            async with sessions.begin() as session:
                await repository.settle_in(session, delta)
                await repository.settle_in(session, delta)
            async with sessions() as session:
                usage = await session.get(LoopBudgetUsage, fixture['loop_id'])
                assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (1, 40, 10)
        finally:
            await _stop(fixture['service'], fixture['loop_id'])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['lane_curator', 'completion_verifier'])
def test_pause_before_result_only_settles_deferred_usage(tmp_path, monkeypatch, kind):
    async def run():
        engine = create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label='rcpt-pause', started_at=datetime.now(UTC))
        config = app_config_for('receipt-pause', None)
        config.models[0].curation_output_method = 'prompt_json'

        async def unavailable_independent_commit(self, delta):
            raise TimeoutError('injected receipt transaction failure before pause')

        class Provider:
            async def ainvoke(self, messages, config):
                config['callbacks'][0].usage_metadata['receipt-pause'] = {'input_tokens': 40, 'output_tokens': 10, 'total_tokens': 50}
                await fixture['service'].control(fixture['loop_id'], 'pause')
                response = {'rationale': '现有Context足够', 'work_specs': []} if kind == 'lane_curator' else {
                    'criteria': [{'check_id': 'tests', 'status': 'unknown', 'explanation': '缺少证据'}], 'conclusion': 'unknown'}
                return AIMessage(content=json.dumps(response))

        monkeypatch.setattr(IndexBudgetReservationRepository, 'settle', unavailable_independent_commit)
        monkeypatch.setattr('backend.app.desktop.agent_loop.structured_worker.create_chat_model', lambda **kwargs: Provider())
        try:
            with pytest.raises((AssertionError, ValueError)):
                await _run_worker(sessions, config, fixture, kind)
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                workers = tuple((await session.scalars(select(LoopWorkerRequest).where(LoopWorkerRequest.loop_id == fixture['loop_id']))).all())
                receipts = tuple((await session.scalars(select(LoopIndexBudgetReservation).where(LoopIndexBudgetReservation.loop_id == fixture['loop_id']))).all())
                assert loop.status == 'paused' and all(row.status == 'cancelled' for row in workers)
                assert len(receipts) == 1 and receipts[0].settled_at is not None
                usage = await session.get(LoopBudgetUsage, fixture['loop_id'])
                assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (1, 40, 10)
        finally:
            await _stop(fixture['service'], fixture['loop_id'])
            await engine.dispose()
    asyncio.run(run())
