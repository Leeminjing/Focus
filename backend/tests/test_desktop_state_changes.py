"""Real PostgreSQL commit/rollback invalidation and bounded generation delivery."""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.persistence.state_changes import ChangeSignal, DesktopStateChanges


def test_generations_preserve_changes_during_snapshot_and_all_subscribers():
    async def run():
        signal = ChangeSignal()
        generation = signal.generation
        listeners = [asyncio.create_task(signal.wait(generation)) for _ in range(2)]
        await asyncio.sleep(0)
        signal.publish()
        assert await asyncio.gather(*listeners) == [1, 1]
        signal.publish()
        assert await signal.wait(generation, 0.01) == 2
        assert await signal.wait(2, 0.01) == 2
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_external_commits_notify_all_listeners_and_rollback_stays_quiet(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine)
        feeds = [DesktopStateChanges(sessions), DesktopStateChanges(sessions)]
        for feed in feeds:
            feed.start()
        try:
            await asyncio.wait_for(asyncio.gather(*(feed.tasks.wait(0) for feed in feeds)), 5)
            cursors = [feed.tasks.generation for feed in feeds]
            identity = uuid.uuid4().hex
            async with engine.connect() as writer:
                await writer.execute(text("INSERT INTO desktop_workspaces (workspace_id, path, display_name) VALUES (:id, :path, 'probe')"),
                                     {"id": identity, "path": str(tmp_path)})
                # BEFORE COMMIT no listener may observe this transaction.
                await asyncio.sleep(0.03)
                assert [feed.tasks.generation for feed in feeds] == cursors
                await writer.commit()
                for feed, cursor in zip(feeds, cursors):
                    assert await feed.tasks.wait(cursor, 2) > cursor
                cursors = [feed.tasks.generation for feed in feeds]
                await writer.execute(text("UPDATE desktop_workspaces SET display_name='rolled back' WHERE workspace_id=:id"), {"id": identity})
                await writer.rollback()
                await asyncio.sleep(0.03)
                assert [feed.tasks.generation for feed in feeds] == cursors
                await writer.execute(text("UPDATE desktop_workspaces SET display_name=display_name WHERE workspace_id=:id"), {"id": identity})
                await writer.commit()
                await asyncio.sleep(0.03)
                assert [feed.tasks.generation for feed in feeds] == cursors
                await writer.execute(text("DELETE FROM desktop_workspaces WHERE workspace_id=:id"), {"id": identity})
                await writer.commit()
        finally:
            await asyncio.gather(*(feed.close() for feed in feeds))
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_listener_reconnect_invalidates_without_polling_application_tables():
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        feed = DesktopStateChanges(async_sessionmaker(engine))
        feed.start()
        try:
            assert await feed.tasks.wait(0, 5) > 0
            generation = feed.tasks.generation
            async with engine.begin() as session:
                await session.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=current_database() AND application_name='focus-desktop-state-listener'"))
            assert await feed.tasks.wait(generation, 5) > generation
        finally:
            await feed.close()
            await engine.dispose()
    asyncio.run(run())
