r"""本文件对外提供 Context revision forward migration 的 PostgreSQL 结构验证。

输入为从空库升级到最新 head 的隔离 PostgreSQL fixture；输出为表、列、外键、检查和唯一约束断言。
具体工作流为执行完整迁移链、反射新 schema，并确认 current pointer 与 revision/source 结构可用。
示例：`python -m pytest backend/tests/test_context_revision_migration.py -q`。
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url


def test_context_revision_forward_migration(isolated_postgres_database) -> None:
    url = make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg")
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        assert {
            "desktop_context_revisions",
            "desktop_context_revision_sources",
        } <= set(inspector.get_table_names())
        thread_columns = {column["name"] for column in inspector.get_columns("desktop_threads")}
        revision_columns = {
            column["name"] for column in inspector.get_columns("desktop_context_revisions")
        }
        assert "current_revision_id" in thread_columns
        assert {
            "revision_id",
            "context_id",
            "generation",
            "execution_thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "payload_mode",
            "content_hash",
            "projection_status",
            "origin_kind",
        } <= revision_columns
        foreign_keys = {
            item["name"] for item in inspector.get_foreign_keys("desktop_context_revision_sources")
        }
        assert foreign_keys == {
            "fk_context_revision_source_context",
            "fk_context_revision_source_revision",
            "fk_context_revision_source_target",
        }
        unique_constraints = {
            item["name"] for item in inspector.get_unique_constraints("desktop_context_revisions")
        }
        assert {
            "uq_context_revision_generation",
            "uq_context_revision_execution_checkpoint",
        } <= unique_constraints
        check_constraints = {
            item["name"] for item in inspector.get_check_constraints("desktop_context_revisions")
        }
        assert {
            "ck_context_revision_generation_positive",
            "ck_context_revision_payload_mode",
            "ck_context_revision_projection_status",
            "ck_context_revision_origin_kind",
            "ck_context_revision_checkpoint_payload",
        } <= check_constraints
    finally:
        engine.dispose()
