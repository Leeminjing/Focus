"""本文件在隔离 PostgreSQL 中复现调度、交付、Journal 与 Run 启动的真实行锁竞态。

屏障只控制生产数据库方法取得首锁后的交错；要求事务均正常完成且保留正确 Run 绑定。
运行：python -m pytest backend/tests/test_loop_dispatch_lock_order.py。
"""

import asyncio
from datetime import UTC, datetime
import os
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.dispatch import LoopWaveDispatcher
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.journal_models import LoopJournalSequence
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDirective, LoopRound
from backend.app.desktop.agent_loop.run_execution import LoopRunExecutionBoundary
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_loop_execution_ownership import _seed_launching_directive, _admit_run

pytestmark = pytest.mark.usefixtures("runtime_postgres_database")


@pytest.mark.parametrize("exhausted", [False, True])
def test_dispatch_and_run_start_share_first_loop_lock(tmp_path, monkeypatch, exhausted):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="dispatch-lock", started_at=datetime.now(UTC),
            budgets={"max_model_calls": 1} if exhausted else {})
        try:
            coordinator = LoopCoordinator(sessions)
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "dispatch-lock-test")
            directive_id = await _seed_launching_directive(sessions, fixture, label="dispatch-lock")
            run_id = await _admit_run(sessions, fixture, directive_id)
            async with sessions.begin() as session:
                current = await session.get(LoopRound, fixture["round_id"])
                current.status = "ready"
                if exhausted:
                    from backend.app.desktop.agent_loop.models import LoopBudgetUsage
                    usage = await session.get(LoopBudgetUsage, fixture["loop_id"])
                    usage.model_calls = 1
            start_execution, execution_locked = asyncio.Event(), asyncio.Event()
            original_get = AsyncSession.get
            first_lock = []

            async def interleave(session, entity, ident, **kwargs):
                value = await original_get(session, entity, ident, **kwargs)
                owner = asyncio.current_task().get_name()
                if kwargs.get("with_for_update"):
                    if owner == "dispatch" and entity in {AgentLoop, LoopRound} and not first_lock:
                        first_lock.append(entity)
                        start_execution.set()
                        if entity is LoopRound:
                            await asyncio.wait_for(execution_locked.wait(), 5)
                    elif owner == "execution" and entity is AgentLoop:
                        execution_locked.set()
                return value

            monkeypatch.setattr(AsyncSession, "get", interleave)

            async def dispatch(*args):
                return (run_id,)

            async def execution():
                await asyncio.wait_for(start_execution.wait(), 5)
                # The budget branch may validly revoke startup before it obtains the loop lock.
                try:
                    async with sessions.begin() as session:
                        _, directive, _ = await LoopRunExecutionBoundary(sessions)._identity(session, run_id, lock=True)
                        await coordinator._directive_lifecycle.transition(session, directive.directive_id, "delivered", run_id=run_id)
                        await coordinator._directive_lifecycle.transition(session, directive.directive_id, "run_started", run_id=run_id)
                except LookupError:
                    assert exhausted

            results = await asyncio.wait_for(asyncio.gather(
                asyncio.create_task(coordinator.dispatch_ready(claim, SimpleNamespace(dispatch=dispatch), 1), name="dispatch"),
                asyncio.create_task(execution(), name="execution"), return_exceptions=True), 15)
            assert not [r for r in results if isinstance(r, BaseException)], results
            assert first_lock == [AgentLoop]
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("writer", ["delivery", "journal"])
def test_delivery_and_journal_do_not_invert_startup_locks(tmp_path, monkeypatch, writer):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="journal-lock", started_at=datetime.now(UTC))
        try:
            directive_id = await _seed_launching_directive(sessions, fixture, label="journal-lock")
            run_id = await _admit_run(sessions, fixture, directive_id)
            start_execution, execution_locked = asyncio.Event(), asyncio.Event()
            original_get = AsyncSession.get
            first_lock = []

            async def interleave(session, entity, ident, **kwargs):
                value = await original_get(session, entity, ident, **kwargs)
                owner = asyncio.current_task().get_name()
                if kwargs.get("with_for_update"):
                    if owner == "writer" and entity in {AgentLoop, LoopDirective, LoopJournalSequence} and not first_lock:
                        first_lock.append(entity)
                        start_execution.set()
                        if entity is not AgentLoop:
                            await asyncio.wait_for(execution_locked.wait(), 5)
                    elif owner == "execution" and entity is AgentLoop:
                        execution_locked.set()
                return value

            monkeypatch.setattr(AsyncSession, "get", interleave)

            async def write():
                if writer == "delivery":
                    await LoopWaveDispatcher(sessions, None)._record_delivery(directive_id, run_id)
                else:
                    async with sessions.begin() as session:
                        await LoopEventJournal().append(session, fixture["loop_id"], CanonicalEventDraft(
                            kind="test.concurrent", entity_type="test", entity_id="writer", entity_revision=1,
                            payload={}, idempotency_key="concurrent-journal-writer"))

            async def execution():
                await asyncio.wait_for(start_execution.wait(), 5)
                async with sessions.begin() as session:
                    _, directive, _ = await LoopRunExecutionBoundary(sessions)._identity(session, run_id, lock=True)
                    lifecycle = LoopCoordinator(sessions)._directive_lifecycle
                    if directive.lifecycle_state == "delivering":
                        await lifecycle.transition(session, directive_id, "delivered", run_id=run_id)
                    await lifecycle.transition(session, directive_id, "run_started", run_id=run_id)

            results = await asyncio.wait_for(asyncio.gather(
                asyncio.create_task(write(), name="writer"),
                asyncio.create_task(execution(), name="execution"), return_exceptions=True), 15)
            assert not [r for r in results if isinstance(r, BaseException)], results
            assert first_lock == [AgentLoop]
            async with sessions() as session:
                directive = await session.get(LoopDirective, directive_id)
                assert directive.lifecycle_state == "run_started"
                assert directive.launched_run_id == run_id
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(run())
