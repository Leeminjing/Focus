"""Exercise retention on an isolated PostgreSQL and backup/preview lifecycle."""
import asyncio
from datetime import UTC, datetime, timedelta
import os
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.event_journal import LoopEventJournal, ReplayUnavailable
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent, LoopProjectorCursor, LoopReplayRetention, LoopLiveCheckpoint
from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure
from backend.app.desktop.storage_maintenance import StorageMaintenance, StoragePolicy
from backend.tests.test_loop_event_journal import _seed_loop, _draft


def test_policy_is_strict_and_bounds_destructive_settings():
    for body in ({"backup_keep_count": 0}, {"journal_retention_days": -1},
                 {"automatic_cleanup": "false"}, {"backup_interval_hours": 1.5}, {"arbitrary": True}):
        with pytest.raises(ValidationError):
            StoragePolicy.model_validate(body)
    assert not StoragePolicy().automatic_cleanup
    assert StoragePolicy().backup_interval_hours == 0


def test_storage_routes_require_local_session_and_validate_policy(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.desktop.storage_routes import storage_router
    from backend.app.gateway.middleware.auth import AuthMiddleware

    app = FastAPI()
    app.include_router(storage_router)
    app.add_middleware(AuthMiddleware)
    app.state.session_key = "storage-test"
    app.state.storage_maintenance = StorageMaintenance(None, home=tmp_path)
    path = "/desktop/api/settings/storage/policy"
    with TestClient(app, client=("127.0.0.1", 10000)) as client:
        assert client.put(path, json={}).status_code == 401
        headers = {"X-Focus-Session": "storage-test"}
        assert client.put(path, headers=headers, json={"backup_keep_count": 0}).status_code == 422
        assert client.put(path, headers=headers, json={"backup_keep_count": 2}).json()["backup_keep_count"] == 2
        response = client.post("/desktop/api/settings/storage/cleanup", headers=headers, json={"token": "unknown"})
        assert response.status_code == 409
    with TestClient(app, client=("203.0.113.5", 10000)) as client:
        assert client.put(path, headers=headers, json={}).status_code == 404


@pytest.mark.usefixtures("isolated_postgres_database")
def test_retention_preserves_pins_consumers_evidence_failures_and_high_water(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        journal = LoopEventJournal(enabled=True)
        loop = await _seed_loop(sessions, tmp_path)
        now = datetime.now(UTC)
        try:
            async with sessions.begin() as session:
                events = [await journal.append(session, loop, _draft(i)) for i in range(6)]
                await session.execute(update(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop)
                                      .values(occurred_at=now - timedelta(days=100)))
                await journal.configure_retention(session, loop, 0)
                assert await journal.prune(session, loop, now=now) == 0
                session.add(LoopProjectorCursor(loop_id=loop, projector_name="loop_facts", last_sequence=4))
                session.add(LoopProjectorCursor(loop_id=loop, projector_name="work_timeline", last_sequence=4))
                await session.execute(update(LoopJournalEvent).where(LoopJournalEvent.event_id == events[2].event_id)
                                      .values(retained_until=now + timedelta(days=1)))
            async with sessions.begin() as session:
                assert await journal.prune(session, loop, now=now) == 2
            async with sessions.begin() as session:
                await session.execute(update(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop).values(retained_until=None))
                fact_id = uuid.uuid4().hex
                session.add(LoopFact(fact_id=fact_id, loop_id=loop, identity_key=fact_id,
                                    fact_type="test", normalized_subject="retention evidence", current_revision=1,
                                    state="observed", evidence=[{"nested": {"event_id": events[5].event_id}}], occurred_at=now))
                await session.flush()
                assert await journal.prune(session, loop, now=now) == 2
            async with sessions.begin() as session:
                await session.execute(update(LoopProjectorCursor).where(LoopProjectorCursor.loop_id == loop).values(last_sequence=6))
                failure_id = uuid.uuid4().hex
                session.add(LoopProjectionFailure(failure_id=failure_id, loop_id=loop, projector_name="loop_facts",
                            unit_kind="journal_event", unit_id=events[4].event_id, boundary_sequence=5,
                            status="quarantined", error_class="ValueError", error_message="repair needed"))
                await session.flush()
                assert await journal.prune(session, loop, now=now) == 0
            async with sessions.begin() as session:
                await session.execute(update(LoopProjectionFailure).where(LoopProjectionFailure.failure_id == failure_id).values(status="resolved"))
                assert await journal.prune(session, loop, now=now) == 1
            async with sessions.begin() as session:
                await session.execute(delete(LoopFact).where(LoopFact.fact_id == fact_id))
                assert await journal.prune(session, loop, now=now) == 1
            async with sessions.begin() as session:
                assert await journal.prune(session, loop, now=now) == 0
                assert (await session.get(LoopReplayRetention, loop)).minimum_sequence == 7
                with pytest.raises(ReplayUnavailable):
                    await journal.read(session, loop, 0)
                next_event = await journal.append(session, loop, _draft(9))
                assert next_event.sequence == 7
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_preview_is_single_use_rechecks_pins_and_saves_checkpoint(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service = StorageMaintenance(sessions, home=tmp_path / "home")
        loop = await _seed_loop(sessions, tmp_path)
        try:
            async with sessions.begin() as session:
                journal = LoopEventJournal(enabled=True)
                events = [await journal.append(session, loop, _draft(i)) for i in range(4)]
                await session.execute(update(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop)
                                      .values(occurred_at=datetime.now(UTC) - timedelta(days=100)))
                session.add(LoopProjectorCursor(loop_id=loop, projector_name="loop_facts", last_sequence=4))
                session.add(LoopProjectorCursor(loop_id=loop, projector_name="work_timeline", last_sequence=4))
            plan = await service.preview()
            assert next(row for row in plan["loops"] if row["loop_id"] == loop)["events"] == 4
            async with sessions.begin() as session:
                await session.execute(update(LoopJournalEvent).where(LoopJournalEvent.event_id == events[2].event_id)
                                      .values(retained_until=datetime.now(UTC) + timedelta(days=1)))
            result = await service.cleanup(plan["token"])
            assert result["journal_events_removed"] == 2
            with pytest.raises(ValueError, match="过期"):
                await service.cleanup(plan["token"])
            async with sessions() as session:
                checkpoint = await session.get(LoopLiveCheckpoint, loop)
                assert checkpoint.last_sequence == 4
                assert (await session.get(LoopReplayRetention, loop)).minimum_sequence == 3
            plan = await service.preview()
            await service.save_policy(StoragePolicy(journal_retention_days=30))
            with pytest.raises(ValueError):
                await service.cleanup(plan["token"])
            assert (await service.status())["policy"]["journal_retention_days"] == 30
            assert not service.backup_available()
            with pytest.raises(ValueError, match="PostgreSQL"):
                await service.create_backup()
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_scheduler_runs_due_backup_and_does_not_repeat_early(tmp_path):
    async def run():
        service = StorageMaintenance(None, home=tmp_path)
        await service.save_policy(StoragePolicy(backup_interval_hours=24))
        service.backup_available = lambda: True
        calls = []
        rows = []
        async def files(command, **payload):
            calls.append(command)
            if command == "backup":
                rows.append({"kind": "regular", "created_at": datetime.now(UTC).isoformat()})
                return {}
            return {"entries": rows}
        service._files = files
        await service.tick()
        await service.tick()
        assert calls == ["inventory", "backup", "inventory"]
        await service.start()
        await service.close()
    asyncio.run(run())


def test_cleanup_reports_committed_work_and_failures_separately(tmp_path):
    async def run():
        service = StorageMaintenance(None, home=tmp_path)
        async def prune(row, policy):
            if row["loop_id"] == "failed":
                raise RuntimeError("fixture transaction failure")
            return 3
        service._prune_loop = prune
        result = await service._apply({"loops": [{"loop_id": "ok"}, {"loop_id": "failed"}],
                                       "policy": {}, "backup_ids": []})
        assert result["journal_events_removed"] == 3
        assert len(result["errors"]) == 1
        assert "failed" in result["errors"][0]
        (tmp_path / "update-state.json").write_text('{"id":"active"}', encoding="utf-8")
        with pytest.raises(ValueError, match="恢复"):
            await service._apply({"loops": [], "backup_ids": []})
    asyncio.run(run())
