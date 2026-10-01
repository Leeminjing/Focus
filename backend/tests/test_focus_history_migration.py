"""本文件对外提供以下合同的验证 V2 additive 迁移对既有历史的无修改保证与安全 downgrade 边界。

输入为隔离 PostgreSQL schema 中的 V1 内容、哈希、来源和 checkpoint 身份；输出为真实 DDL 前后一致性断言。
具体工作流为建立 V1 fixture、执行正式迁移、对比完整旧行，再写入 V2 并确认 destructive downgrade 被拒绝。
示例：pytest backend/tests/test_focus_history_migration.py；不接触用户数据库中的 Context。
"""

import importlib
import os
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.mark.usefixtures("isolated_postgres_database")
def test_additive_history_migration_preserves_v1_and_refuses_v2_downgrade():
    schema = "history_migration_" + uuid.uuid4().hex
    engine = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg"))
    migration = importlib.import_module("focus.persistence.migrations.versions.c03b4c5d6e7f_focus_item_history")
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text("CREATE TABLE desktop_context_revisions (revision_id TEXT, authored_messages JSONB, execution_messages JSONB, content_hash TEXT, sources JSONB, checkpoint_id TEXT)"))
            connection.execute(text("INSERT INTO desktop_context_revisions VALUES ('v1', '[{\"role\":\"human\",\"content\":\"original\"}]', '[]', 'immutable-hash', '[{\"revision_id\":\"parent\"}]', 'exact-cp')"))
            before = connection.execute(text("SELECT to_jsonb(r) FROM desktop_context_revisions r")).scalar_one()
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            after = connection.execute(text("SELECT to_jsonb(r) FROM desktop_context_revisions r")).scalar_one()
            assert after.pop("history_payload") is None
            assert after == before
            connection.execute(text("UPDATE desktop_context_revisions SET history_payload = 'null'::jsonb"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                migration.upgrade()
            connection.execute(text("UPDATE desktop_context_revisions SET history_payload = CAST(:payload AS jsonb)"),
                               {"payload": '{"schema_version":2,"execution_items":[]}'})
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="V2"):
                    migration.downgrade()
            assert connection.execute(text("SELECT history_payload FROM desktop_context_revisions")).scalar_one()["schema_version"] == 2
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()
