"""验证 Run outbox 旧事件/凭据在升级、恢复和回退中保留。"""

import asyncio
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.run_orchestration.outbox import RunOutboxConsumer


def test_upgrade_recovers_legacy_claim_and_downgrade_preserves_events():
    # migration_support supplies a separate database for command.downgrade tests.
    configuration = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    engine = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg"))
    workspace, context, run_id, loop_id = (uuid.uuid4().hex for _ in range(4))
    events = {status: uuid.uuid4().hex for status in ("pending", "error", "claimed", "delivered")}
    try:
        command.downgrade(configuration, "e25c6d7e8f90")
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO desktop_workspaces(workspace_id,path,display_name) VALUES(:id,:path,'outbox')"),
                {"id": workspace, "path": f"/tmp/{workspace}"})
            connection.execute(text("INSERT INTO desktop_threads(task_id,workspace_id,thread_id,title) VALUES(:id,:workspace,:id,'outbox')"),
                {"id": context, "workspace": workspace})
            connection.execute(text("INSERT INTO desktop_runs(run_id,task_id,agent_id,kind,status,loop_id) VALUES(:id,:context,'main','main','success',:loop)"),
                {"id": run_id, "context": context, "loop": loop_id})
            for status, event_id in events.items():
                connection.execute(text("INSERT INTO run_outbox_events(event_id,run_id,event_type,payload,status,delivery_count,claimed_by,error) "
                    "VALUES(:id,:run,:type,CAST(:payload AS jsonb),:status,1,:owner,:error)"),
                    {"id": event_id, "run": run_id, "type": status, "payload": '{"original": true}',
                     "status": status, "owner": "dead" if status == "claimed" else None, "error": "old error" if status == "error" else None})
            connection.execute(text("INSERT INTO run_outbox_deliveries(delivery_id,event_id,consumer_id) VALUES(:id,:event,'coordinator')"),
                {"id": uuid.uuid4().hex, "event": events["delivered"]})
        command.upgrade(configuration, "head")
        columns = {column["name"] for column in inspect(engine).get_columns("run_outbox_events")}
        assert {"lease_expires_at", "next_attempt_at"} <= columns
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT event_id,status,delivery_count,payload,error FROM run_outbox_events")).mappings().all()
            assert len(rows) == 4
            assert {row["status"] for row in rows} == set(events)
            assert all(row["delivery_count"] == 1 and row["payload"] == {"original": True} for row in rows)
            assert next(row for row in rows if row["status"] == "error")["error"] == "old error"

        async def recover():
            async_engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
            try:
                consumer = RunOutboxConsumer(async_sessionmaker(async_engine, expire_on_commit=False))
                assert await consumer.drain("coordinator", lambda *_: None, loop_id=loop_id) == 3
                assert await consumer.drain("coordinator", lambda *_: None, loop_id=loop_id) == 0
            finally:
                await async_engine.dispose()

        asyncio.run(recover())
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT count(*) FROM run_outbox_deliveries")) == 4
            connection.execute(text("UPDATE run_outbox_events SET status='failed',error='persistent failure' WHERE event_id=:id"), {"id": events["claimed"]})
        command.downgrade(configuration, "e25c6d7e8f90")
        assert "lease_expires_at" not in {column["name"] for column in inspect(engine).get_columns("run_outbox_events")}
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM run_outbox_events")) == 4
            assert connection.scalar(text("SELECT count(*) FROM run_outbox_deliveries")) == 4
            row = connection.execute(text("SELECT status,error,delivery_count FROM run_outbox_events WHERE event_id=:id"), {"id": events["claimed"]}).one()
            assert tuple(row) == ("error", "persistent failure", 2)
        command.upgrade(configuration, "head")
    finally:
        engine.dispose()
