"""运行事件处理回滚后，当前进程必须能重试且只提交一次数据库副作用。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import os
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.run_orchestration.models import RunOutboxDelivery, RunOutboxEvent
from backend.app.desktop.run_orchestration.outbox import RunOutboxConsumer, RunOutboxRepository
from backend.app.desktop.agent_loop.supervisor import LoopSupervisor, SupervisorComponent


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 17, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@asynccontextmanager
async def _outbox(event_type="MainRunSettled", *, expire_on_commit=False):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=expire_on_commit)
    workspace_id, context_id, run_id, loop_id = (uuid.uuid4().hex for _ in range(4))
    event_id = RunOutboxRepository.event_id(run_id, event_type)
    try:
        async with sessions.begin() as session:
            session.add(DesktopWorkspace(workspace_id=workspace_id, path=f"/tmp/{workspace_id}", display_name="Outbox"))
            await session.flush()
            session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=context_id, title="Outbox"))
            await session.flush()
            session.add(DesktopRun(run_id=run_id, task_id=context_id, agent_id="main", kind="main", status="success", loop_id=loop_id))
            await session.flush()
            session.add(RunOutboxEvent(event_id=event_id, run_id=run_id, event_type=event_type, payload={}))
        yield sessions, event_id, run_id, loop_id
    finally:
        async with sessions.begin() as session:
            await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id))
        await engine.dispose()


@pytest.mark.parametrize("event_type", ["MainRunSettled", "ArtifactVersion"])
def test_handler_failure_rolls_back_and_retries_without_restart(event_type):
    async def run():
        async with _outbox(event_type) as (sessions, event_id, run_id, loop_id):
            clock = _Clock()
            consumer = RunOutboxConsumer(sessions, RunOutboxRepository(clock=clock))
            calls = 0

            async def handle(event, session):
                nonlocal calls
                calls += 1
                row = await session.get(DesktopRun, event.run_id)
                row.workspace_result = {"applied": row.workspace_result.get("applied", 0) + 1}
                await session.flush()
                if calls == 1:
                    raise RuntimeError("transient handler failure")

            with pytest.raises(RuntimeError, match="transient handler failure"):
                await consumer.drain("coordinator", handle, loop_id=loop_id)
            async with sessions() as session:
                event = await session.get(RunOutboxEvent, event_id)
                assert event.status == "error", "处理回滚后不能遗留 claimed"
                assert event.error == "RuntimeError: transient handler failure"
                assert event.claimed_by is None and event.lease_expires_at is None
                assert event.next_attempt_at == clock.now + timedelta(seconds=1)
                assert (await session.get(DesktopRun, run_id)).workspace_result == {}
                assert not list(await session.scalars(select(RunOutboxDelivery).where(RunOutboxDelivery.event_id == event_id)))
            assert await consumer.drain("coordinator", handle, loop_id=loop_id) == 0
            clock.advance(1)
            assert await consumer.drain("coordinator", handle, loop_id=loop_id) == 1
            assert await consumer.drain("coordinator", handle, loop_id=loop_id) == 0
            assert calls == 2
            async with sessions() as session:
                event = await session.get(RunOutboxEvent, event_id)
                assert event.status == "delivered" and event.delivery_count == 2
                assert event.error is None and event.next_attempt_at is None
                assert (await session.get(DesktopRun, run_id)).workspace_result == {"applied": 1}
                assert len(list(await session.scalars(select(RunOutboxDelivery).where(RunOutboxDelivery.event_id == event_id)))) == 1

    asyncio.run(run())


def test_supervisor_retries_handler_in_same_process():
    async def run():
        async with _outbox() as (sessions, event_id, run_id, loop_id):
            clock = _Clock()
            consumer = RunOutboxConsumer(sessions, RunOutboxRepository(clock=clock))
            delivered = asyncio.Event()
            calls = 0

            async def handle(event, session):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("first tick fails")
                (await session.get(DesktopRun, event.run_id)).workspace_result = {"applied": 1}

            async def tick():
                clock.advance(1)
                if await consumer.drain("coordinator", handle, loop_id=loop_id):
                    delivered.set()

            supervisor = LoopSupervisor("outbox-test", [SupervisorComponent("run_events", tick, 0.001)])
            await supervisor.start()
            try:
                await asyncio.wait_for(delivered.wait(), 5)
                assert calls == 2
                async with sessions() as session:
                    assert (await session.get(RunOutboxEvent, event_id)).status == "delivered"
                    assert (await session.get(DesktopRun, run_id)).workspace_result == {"applied": 1}
            finally:
                await supervisor.close()

    asyncio.run(run())


def test_cancelled_handler_returns_claim_and_preserves_cancellation():
    async def run():
        async with _outbox(expire_on_commit=True) as (sessions, event_id, _run_id, loop_id):
            clock = _Clock()
            consumer = RunOutboxConsumer(sessions, RunOutboxRepository(clock=clock))
            entered = asyncio.Event()

            async def blocked(_event, _session):
                entered.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(consumer.drain("coordinator", blocked, loop_id=loop_id))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with sessions() as session:
                event = await session.get(RunOutboxEvent, event_id)
                assert event.status == "error" and event.error.startswith("CancelledError")
                assert event.claimed_by is None
            clock.advance(1)
            assert await consumer.drain("coordinator", lambda *_: None, loop_id=loop_id) == 1

    asyncio.run(run())


@pytest.mark.parametrize("same_owner", [False, True])
def test_expired_claim_is_recovered_by_drain_and_old_claim_is_fenced(same_owner):
    async def run():
        async with _outbox() as (sessions, event_id, _run_id, loop_id):
            clock = _Clock()
            repository = RunOutboxRepository(clock=clock, lease_seconds=5)
            consumer = RunOutboxConsumer(sessions, repository)
            owner = "old" if same_owner else "replacement"
            async with sessions.begin() as session:
                first = await repository.claim_next(session, "old", loop_id)
                first_attempt = first.delivery_count
            assert await consumer.recover() == 0  # A different process starting cannot steal this lease.
            clock.advance(5)
            # The normal tick recovers the abandoned claim and applies durable backoff.
            assert await consumer.drain(owner, lambda *_: None, loop_id=loop_id) == 0
            clock.advance(1)
            async with sessions.begin() as session:
                second = await repository.claim_next(session, owner, loop_id)
                second_attempt = second.delivery_count
                assert second_attempt == first_attempt + 1
            async with sessions.begin() as session:
                assert not await repository.fail_claim(session, event_id, "wrong-owner", second_attempt, "wrong owner")
                assert not await repository.deliver_once(session, event_id, "wrong-owner", lambda *_: pytest.fail("wrong owner ran"), claim_attempt=second_attempt)
                assert not await repository.fail_claim(session, event_id, "old", first_attempt, "late failure")
                assert not await repository.deliver_once(session, event_id, "old", lambda *_: pytest.fail("stale handler ran"), claim_attempt=first_attempt)
            async with sessions.begin() as session:
                event = await session.get(RunOutboxEvent, event_id)
                assert event.status == "claimed" and event.claimed_by == owner
                assert await repository.deliver_once(session, event_id, owner, lambda *_: None, claim_attempt=second_attempt)
            assert await consumer.drain(owner, lambda *_: pytest.fail("duplicate"), loop_id=loop_id) == 0

    asyncio.run(run())


def test_scoped_recovery_only_releases_expired_claims_for_its_loop():
    async def run():
        async with _outbox() as (sessions, first_id, _run_id, first_loop):
            async with _outbox() as (_other_sessions, second_id, _other_run, second_loop):
                clock = _Clock()
                repository = RunOutboxRepository(clock=clock, lease_seconds=5)
                consumer = RunOutboxConsumer(sessions, repository)
                async with sessions.begin() as session:
                    await repository.claim_next(session, "first", first_loop)
                    await repository.claim_next(session, "second", second_loop)
                clock.advance(5)
                assert await consumer.drain("first", lambda *_: None, loop_id=first_loop) == 0
                async with sessions() as session:
                    assert (await session.get(RunOutboxEvent, first_id)).status == "error"
                    assert (await session.get(RunOutboxEvent, second_id)).status == "claimed"

    asyncio.run(run())


def test_repeated_abandoned_claims_also_reach_failed_state():
    async def run():
        async with _outbox() as (sessions, event_id, _run_id, loop_id):
            clock = _Clock()
            repository = RunOutboxRepository(clock=clock, lease_seconds=5, max_attempts=2)
            consumer = RunOutboxConsumer(sessions, repository)
            for _ in range(2):
                async with sessions.begin() as session:
                    assert await repository.claim_next(session, "abandoned", loop_id)
                clock.advance(5)
                assert await consumer.drain("replacement", lambda *_: pytest.fail("not due"), loop_id=loop_id) == 0
                clock.advance(1)
            async with sessions() as session:
                event = await session.get(RunOutboxEvent, event_id)
                assert event.status == "failed" and event.delivery_count == 2
                assert event.error == "claim lease expired" and event.next_attempt_at is None

    asyncio.run(run())


def test_recovery_skips_locked_handler_even_after_lease_expires():
    async def run():
        async with _outbox() as (sessions, event_id, _run_id, loop_id):
            clock = _Clock()
            repository = RunOutboxRepository(clock=clock, lease_seconds=5)
            consumer = RunOutboxConsumer(sessions, repository)
            entered, finish = asyncio.Event(), asyncio.Event()

            async def handle(_event, _session):
                entered.set()
                await finish.wait()

            task = asyncio.create_task(consumer.drain("first", handle, loop_id=loop_id))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                clock.advance(6)
                other = RunOutboxConsumer(sessions, repository)
                assert await asyncio.wait_for(other.recover(), 2) == 0
                assert await asyncio.wait_for(other.drain("second", lambda *_: pytest.fail("stolen"), loop_id=loop_id), 2) == 0
                finish.set()
                assert await asyncio.wait_for(task, 5) == 1
                async with sessions() as session:
                    assert (await session.get(RunOutboxEvent, event_id)).delivery_count == 1
            finally:
                finish.set()
                await task

    asyncio.run(run())


def test_persistent_failure_backs_off_then_stops_without_blocking_other_events():
    async def run():
        async with _outbox() as (sessions, event_id, run_id, loop_id):
            clock = _Clock()
            repository = RunOutboxRepository(clock=clock, max_attempts=4, max_retry_seconds=3)
            consumer = RunOutboxConsumer(sessions, repository)

            def fail(*_):
                raise ValueError("poison event")

            for attempt, delay in enumerate((1, 2, 3, None), 1):
                with pytest.raises(ValueError, match="poison event"):
                    await consumer.drain("coordinator", fail, loop_id=loop_id)
                async with sessions() as session:
                    event = await session.get(RunOutboxEvent, event_id)
                    assert event.delivery_count == attempt
                    assert event.status == ("error" if delay else "failed")
                    assert event.error == "ValueError: poison event"
                    assert event.next_attempt_at == (clock.now + timedelta(seconds=delay) if delay else None)
                assert await consumer.drain("coordinator", fail, loop_id=loop_id) == 0
                if attempt == 1:
                    async with sessions.begin() as session:
                        await repository.enqueue_artifact(session, run_id, "healthy-result")
                    assert await consumer.drain("coordinator", lambda *_: None, loop_id=loop_id) == 1
                clock.advance(delay or 1000)
            # A new process does not reset durable retries or resurrect failed work.
            restarted = RunOutboxConsumer(sessions, repository)
            assert await restarted.recover() == 0
            assert await restarted.drain("coordinator", fail, loop_id=loop_id) == 0

    asyncio.run(run())


def test_lease_recovers_when_exception_cleanup_cannot_access_database():
    async def run():
        async with _outbox() as (sessions, event_id, _run_id, loop_id):
            clock = _Clock()

            class CleanupFailure(RunOutboxRepository):
                async def fail_claim(self, *_args):
                    raise ConnectionError("database temporarily unavailable")

            repository = CleanupFailure(clock=clock, lease_seconds=5)
            consumer = RunOutboxConsumer(sessions, repository)

            def fail(*_):
                raise RuntimeError("original handler failure")

            with pytest.raises(RuntimeError, match="original handler failure"):
                await consumer.drain("coordinator", fail, loop_id=loop_id)
            async with sessions() as session:
                assert (await session.get(RunOutboxEvent, event_id)).status == "claimed"
            clock.advance(5)
            assert await consumer.drain("coordinator", lambda *_: None, loop_id=loop_id) == 0
            clock.advance(1)
            assert await consumer.drain("coordinator", lambda *_: None, loop_id=loop_id) == 1

    asyncio.run(run())
