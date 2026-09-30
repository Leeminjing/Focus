"""本文件对外提供记忆迁移的隔离升级/降级/恢复测试。

输入为独立随机命名的 PostgreSQL 数据库与旧 Observation；输出为旧正文/hash 保留和新证明可重建的断言。
具体工作流为升级、播种旧记录、仅降级新增迁移、再升级；销毁仅针对本测试创建的数据库。
示例：python -m pytest backend/tests/test_task_memory_migration.py -q。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_agent_loop_round_liveness import _seed_loop
from test_patrol_session import _observation_envelope

from backend.app.desktop.agent_loop.models import LoopObservation
from backend.app.desktop.context_evolution.models import ContextPublicationReceipt


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
            fixture = await _seed_loop(
                sessions, tmp_path, label="migration", started_at=datetime.now(UTC)
            )
            envelope = _observation_envelope(
                fixture["loop_id"], fixture["round_id"]
            ).model_dump(mode="json")
            observation_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(
                    LoopObservation(
                        observation_id=observation_id,
                        loop_id=fixture["loop_id"],
                        round_id=fixture["round_id"],
                        envelope=envelope,
                        envelope_hash="b" * 64,
                        projection_sequence=0,
                        base_entity_revisions={},
                    )
                )
            return observation_id, envelope, fixture["context_id"]
        finally:
            await engine.dispose()

    async def verify(observation_id, envelope, context_id):
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        try:
            async with async_sessionmaker(engine)() as session:
                row = await session.get(LoopObservation, observation_id)
                assert row.envelope == envelope and row.envelope_hash == "b" * 64
                assert await session.scalar(
                    select(ContextPublicationReceipt.revision_id).where(
                        ContextPublicationReceipt.context_id == context_id
                    )
                )
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
        command.upgrade(config, "head")
        identity = asyncio.run(seed())
        command.downgrade(config, "7b8c9d0e1f2a")
        command.upgrade(config, "head")
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
