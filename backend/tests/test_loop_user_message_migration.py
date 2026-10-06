"""本文件对外提供用户消息增量迁移的独立 PostgreSQL 验证。

输入为随机隔离数据库中的历史意见或真实受理原文；输出为旧数据默认类型、无损升级和拒绝丢失审计的断言。
具体工作流为沿正式服务种下 Loop，执行 Alembic 降级和升级，核对原文、请求 hash 与版本保留。
示例：pytest backend/tests/test_loop_user_message_migration.py -q；不连接原生实验或生产数据库。
"""

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from backend.app.desktop.agent_loop.models import LoopUserIntent
from backend.tests.test_agent_loop_round_liveness import _seed_loop


async def _seed(tmp_path, direct):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="user-migration", started_at=datetime.now(UTC))
        if direct:
            return (await fixture["service"].user_message(fixture["context_id"], "原文\n保留"))["intent_id"]
        identity = uuid.uuid4().hex
        async with sessions.begin() as session:
            session.add(LoopUserIntent(intent_id=identity, loop_id=fixture["loop_id"], scope="portfolio",
                content="历史意见", correlation_id=identity, goal_revision=1, authority_revision=1))
        return identity
    finally:
        await engine.dispose()


@pytest.mark.parametrize("direct", [False, True])
def test_user_message_migration_preserves_history_or_refuses_lossy_rollback(tmp_path, isolated_postgres_database, direct):
    identity = asyncio.run(_seed(tmp_path, direct))
    config = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    engine = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg"))
    try:
        with Session(engine) as session:
            original = session.get(LoopUserIntent, identity)
            payload, content = original.request_payload, original.content
        if direct:
            with pytest.raises(RuntimeError, match="原始请求审计"):
                command.downgrade(config, "d47a8b9c0d1e")
        else:
            command.downgrade(config, "d47a8b9c0d1e")
            assert "intent_kind" not in {column["name"] for column in inspect(engine).get_columns("loop_user_intents")}
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT content FROM loop_user_intents WHERE intent_id=:id"), {"id": identity}) == content
            command.upgrade(config, "head")
        with Session(engine) as session:
            restored = session.get(LoopUserIntent, identity)
            assert restored.content == content and restored.request_payload == payload
            assert restored.intent_kind == ("direct_message" if direct else "patrol_opinion")
            assert session.scalar(text("SELECT version_num FROM alembic_version")) == "e58b9c0d1e2f"
    finally:
        engine.dispose()
