"""Commit-to-consumer latency through real PostgreSQL, without model execution.

The decision probe uses Kernel, ContextRunPool, Dispatcher and the real Run start
boundary with a model-free executor. The Worker probe reads the committed
result. Timings exclude model requests and are not model/end-to-end benchmarks.
"""

import asyncio
from datetime import UTC, datetime
import json
import os
from time import perf_counter
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from backend.app.desktop.patrol.agent_loop import (
    ContextRunPool, LoopCoordinator, LoopCoordinatorRuntime, LoopKernel,
    LoopWaveDispatcher, PatrolDecisionIntent,
)
from backend.app.desktop.patrol.agent_loop.models import LoopWorkerRequest
from backend.app.desktop.runs.orchestration import RunOutboxConsumer
from backend.app.desktop.persistence.models import DesktopRun
from backend.app.desktop.persistence.state_changes import PostgresNotifications
from backend.tests.test_loop_runtime_pools import _create_loop, _frontier
from loop_run_boundary_support import freeze_patrol_intent, start_loop_run


@pytest.mark.usefixtures("isolated_postgres_database")
def test_commit_and_rollback_routing_with_external_writer(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        ready, arrived = asyncio.Event(), asyncio.Event()
        hints = []

        def notify(payload):
            hints.append(json.loads(payload))
            arrived.set()

        feed = PostgresNotifications(sessions, {"focus_loop_work": notify}, ready.set,
                                     application_name="focus-loop-routing-test")
        service, snapshot, _, _ = await _create_loop(sessions, tmp_path)
        feed.start()
        try:
            await asyncio.wait_for(ready.wait(), 5)
            async with engine.connect() as writer:
                params = {"id": snapshot["current_round_id"]}
                await writer.execute(text("UPDATE loop_rounds SET status='curated' WHERE round_id=:id"), params)
                await asyncio.sleep(0.03)
                assert not hints
                await writer.rollback()
                await asyncio.sleep(0.03)
                assert not hints
                # No-op UPDATEs and unrelated changes must not form a hot loop.
                await writer.execute(text("UPDATE loop_rounds SET status=status WHERE round_id=:id"), params)
                await writer.commit()
                await asyncio.sleep(0.03)
                assert not hints
                await writer.execute(text("UPDATE loop_rounds SET status='curated' WHERE round_id=:id"), params)
                await writer.commit()
                await asyncio.wait_for(arrived.wait(), 1)
                assert hints == [{"loop_id": snapshot["loop_id"], "components": ["rounds", "context_runs"]}]
        finally:
            await feed.close()
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.usefixtures("runtime_postgres_database")
@pytest.mark.parametrize("boundary", ["decision_to_run", "worker_to_round"])
@pytest.mark.parametrize("notifications", [True, False])
def test_committed_work_starts_before_recovery_poll(tmp_path, boundary, notifications):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot, context_id, revision_id = await _create_loop(sessions, tmp_path)
        loop_id, round_id = snapshot["loop_id"], snapshot["current_round_id"]
        ready, idle, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        times = {}
        worker_id = uuid.uuid4().hex

        class Coordinator(LoopCoordinator):
            async def dispatch_ready(self, claim, dispatcher, concurrency):
                times.setdefault("admitted", perf_counter())
                return await super().dispatch_ready(claim, dispatcher, concurrency)

            async def claim_for_loop(self, identity, _owner):
                if boundary == "worker_to_round" and identity == loop_id:
                    async with sessions() as session:
                        worker = await session.get(LoopWorkerRequest, worker_id)
                        if worker is not None and worker.status == "success":
                            times.setdefault("started", perf_counter())
                            started.set()
                    idle.set()
                return None

        class Pool(ContextRunPool):
            async def drain(self, identity=None):
                count = await super().drain(identity)
                if boundary == "decision_to_run":
                    idle.set()
                return count

        async def launch(directive, _message, _slot):
            run_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(DesktopRun(run_id=run_id, task_id=context_id,
                    agent_id=f"main:{context_id}", kind="main", status="pending",
                    origin="delegated_patrol", execution_thread_id=f"latency-{run_id}",
                    context_revision_id=revision_id, loop_id=loop_id, round_id=round_id,
                    directive_id=directive.directive_id, equipment={"permissions": ["read"]}))
            await start_loop_run(sessions, run_id)
            times.setdefault("started", perf_counter())
            started.set()
            return run_id

        async def recover():
            pass

        coordinator = Coordinator(sessions)
        dispatcher = LoopWaveDispatcher(sessions, launch)
        pool = Pool(sessions, coordinator, dispatcher)
        runtime = LoopCoordinatorRuntime(coordinator, RunOutboxConsumer(sessions),
            context_runs=pool, recovery=SimpleNamespace(reconcile=recover),
            poll_seconds=30 if notifications else 1)
        if not notifications:
            runtime._notifications._channels = {"focus_loop_work": lambda _payload: None}
        original_ready = runtime._notifications._on_ready

        def listening():
            original_ready()
            ready.set()

        runtime._notifications._on_ready = listening

        def committed(session):
            if session.info.get("latency_boundary"):
                times["committed"] = perf_counter()

        class Kernel(LoopKernel):
            async def _commit_new(self, session, intent):
                result = await super()._commit_new(session, intent)
                session.info["latency_boundary"] = True
                return result

        event.listen(Session, "after_commit", committed)
        try:
            if boundary == "worker_to_round":
                async with sessions.begin() as session:
                    session.add(LoopWorkerRequest(worker_request_id=worker_id, loop_id=loop_id,
                        round_id=round_id, kind="lane_curator", scope={}, status="running"))
            else:
                intent = await freeze_patrol_intent(sessions, PatrolDecisionIntent(
                    decision_id=uuid.uuid4().hex, idempotency_key=f"latency:{round_id}",
                    loop_id=loop_id, loop_revision=snapshot["revision"], round_id=round_id,
                    holder_id=snapshot["holder_id"], grant_id=snapshot["grant"]["grant_id"],
                    grant_revision=1, goal_revision=1,
                    observed_frontier_hash=await _frontier(sessions, round_id),
                    observed_workspace_revision=1, rationale="Measure scheduling only.",
                    actions=({"action": "continue_context", "context_id": context_id,
                        "context_revision_id": revision_id, "message": "Continue."},)))
            await runtime.start()
            await asyncio.wait_for(ready.wait(), 5)
            await asyncio.wait_for(idle.wait(), 1)
            # Preparation precedes startup so the polling control commits near
            # the beginning of its sleep, rather than at an arbitrary poll phase.
            await asyncio.sleep(0.05)
            if boundary == "decision_to_run":
                result = await Kernel(sessions).commit(intent)
                assert result.status == "committed", result
            else:
                async with sessions.begin() as session:
                    worker = await session.get(LoopWorkerRequest, worker_id)
                    worker.status, worker.completed_at = "success", datetime.now(UTC)
                    session.info["latency_boundary"] = True
            await asyncio.wait_for(started.wait(), 1 if notifications else 2)
            elapsed_ms = (times["started"] - times["committed"]) * 1000
            assert 0 <= elapsed_ms < (1000 if notifications else 2000)
            scheduling_ms = (times.get("admitted", times["started"]) - times["committed"]) * 1000
            mode = "notify (poll=30000 ms)" if notifications else "poll only (poll=1000 ms)"
            print(f"{boundary}, {mode}: {elapsed_ms:.2f} ms; commit-to-admission={scheduling_ms:.2f} ms (model=excluded)")
        finally:
            event.remove(Session, "after_commit", committed)
            await runtime.close()
            await service.control(loop_id, "stop")
            await engine.dispose()

    asyncio.run(run())


def test_runtime_routes_hints_and_ignores_malformed_payloads():
    runtime = LoopCoordinatorRuntime(None, None)
    calls = []
    runtime._registry.wake = lambda *args: calls.append(args)
    runtime._work_committed('{"loop_id":"one","components":["rounds"]}')
    runtime._work_committed('{"loop_id":null,"components":["workers"]}')
    for payload in ("not json", "null", "[]", '{}', '{"loop_id":1,"components":[]}',
                    '{"loop_id":"one","components":[{}]}'):
        runtime._work_committed(payload)
    assert calls == [("one", "rounds"), (None, "workers")]


@pytest.mark.usefixtures("isolated_postgres_database")
def test_reconnect_reconciles_work_missed_during_disconnect():
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        ready = asyncio.Queue()
        feed = PostgresNotifications(async_sessionmaker(engine), {"focus_loop_work": lambda _payload: None},
            lambda: ready.put_nowait(True), application_name="focus-loop-reconnect-test")
        feed.start()
        try:
            await asyncio.wait_for(ready.get(), 5)
            async with engine.begin() as writer:
                await writer.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=current_database() AND application_name='focus-loop-reconnect-test'"))
            await asyncio.wait_for(ready.get(), 5)
        finally:
            await feed.close()
            await engine.dispose()

    asyncio.run(run())


def test_notification_migration_round_trip():
    # migration_support supplies a dedicated disposable database for downgrade.
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    command.downgrade(config, "e36d7e8f9012")
    command.upgrade(config, "head")
