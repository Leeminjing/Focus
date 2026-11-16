"""本文件对外提供记忆迁移的隔离升级/降级/恢复测试。

输入为独立随机命名的 PostgreSQL 数据库与旧 Observation；输出为旧正文/hash 保留和新证明可重建的断言。
具体工作流为升级、播种旧记录、仅降级新增迁移、再升级；销毁仅针对本测试创建的数据库。
示例：python -m pytest backend/tests/test_task_memory_migration.py -q。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_patrol_session import _observation_envelope

from backend.app.desktop.persistence.models import DesktopThread


def test_additive_migration_preserves_historical_observation(tmp_path, monkeypatch):
    database_name = f"focus_memory_smoke_{uuid.uuid4().hex}"
    original_url = make_url(os.environ["FOCUS_DATABASE_URL"])
    admin = create_engine(
        original_url.set(drivername="postgresql+psycopg", database="postgres", query={})
    )
    migrations = (
        Path(__file__).parents[1]
        / "packages/harness/focus/persistence/migrations/alembic.ini"
    )
    config = Config(str(migrations))
    created = False

    async def seed():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            # Seed the historical schema with its own SQL contract. Current-state
            # publishers intentionally cannot write the retired archive tables.
            fixture = {key: uuid.uuid4().hex for key in ("workspace_id", "context_id", "loop_id", "round_id", "revision_id")}
            async with sessions.begin() as session:
                await session.execute(text("INSERT INTO desktop_workspaces(workspace_id,path,display_name) VALUES (:workspace_id,:path,'Migration')"), {**fixture, "path": str(tmp_path / fixture["workspace_id"])})
                await session.execute(text("INSERT INTO desktop_threads(task_id,workspace_id,thread_id,title) VALUES (:context_id,:workspace_id,:context_id,'Migration')"), fixture)
                await session.execute(text("INSERT INTO desktop_context_revisions (revision_id,context_id,generation,execution_thread_id,checkpoint_id,payload_mode,content_hash,projection_status,origin_kind) VALUES (:revision_id,:context_id,1,:context_id,'checkpoint','checkpoint',:content_hash,'valid','root')"), {**fixture, "content_hash": "a" * 64})
                await session.execute(text("UPDATE desktop_threads SET current_revision_id=:revision_id WHERE task_id=:context_id"), fixture)
                await session.execute(text("INSERT INTO agent_loops(loop_id,workspace_id,initial_context_id,holder_id,status) VALUES (:loop_id,:workspace_id,:context_id,'migration','running')"), fixture)
                await session.execute(text("INSERT INTO loop_rounds(round_id,loop_id,number,status,authority_revision,goal_revision,frontier_hash,workspace_revision) VALUES (:round_id,:loop_id,1,'observed',1,1,:frontier_hash,1)"), {**fixture, "frontier_hash": "a" * 64})
            envelope = _observation_envelope(
                fixture["loop_id"], fixture["round_id"]
            ).model_dump(mode="json")
            observation_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                await session.execute(text("INSERT INTO loop_observations(observation_id,loop_id,round_id,envelope,envelope_hash,projection_sequence,base_entity_revisions) VALUES (:observation_id,:loop_id,:round_id,CAST(:envelope AS jsonb),:envelope_hash,0,'{}'::jsonb)"), {**fixture, "observation_id": observation_id, "envelope": json.dumps(envelope), "envelope_hash": "b" * 64})
            return observation_id, envelope, fixture["context_id"]
        finally:
            await engine.dispose()

    async def verify(observation_id, envelope, context_id):
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        try:
            async with async_sessionmaker(engine)() as session:
                row = (await session.execute(text("SELECT envelope,envelope_hash FROM loop_observations WHERE observation_id=:identity"), {"identity": observation_id})).mappings().one()
                assert row["envelope"] == envelope and row["envelope_hash"] == "b" * 64
                assert await session.scalar(select(DesktopThread.current_revision_id).where(DesktopThread.task_id == context_id))
        finally:
            await engine.dispose()

    try:
        with admin.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        created = True
        monkeypatch.setenv(
            "FOCUS_DATABASE_URL",
            original_url.set(database=database_name).render_as_string(
                hide_password=False
            ),
        )
        command.upgrade(config, "8c9d0e1f2a3b")
        identity = asyncio.run(seed())
        command.downgrade(config, "7b8c9d0e1f2a")
        command.upgrade(config, "8c9d0e1f2a3b")
        asyncio.run(verify(*identity))
    finally:
        if created:
            with admin.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.execute(
                    text(f'DROP DATABASE "{database_name}" WITH (FORCE)')
                )
        admin.dispose()
