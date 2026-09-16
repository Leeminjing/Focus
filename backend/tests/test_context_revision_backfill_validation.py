r"""本文件对外提供 Context revision 回填前 legacy 一致性失败与事务原子性验证。

输入为跨工作区、错 checkpoint、缺 definition、重复位置和错 current pointer 的旧 fixtures；输出为整体拒绝断言。
具体工作流为在结构迁移上注入全部损坏形态、运行回填、检查分类错误且没有 migration revision。
示例：`python -m pytest backend/tests/test_context_revision_backfill_validation.py -q`。
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


MIGRATIONS = (
    Path(__file__).parents[1]
    / "packages"
    / "harness"
    / "focus"
    / "persistence"
    / "migrations"
    / "alembic.ini"
)


def test_corrupt_legacy_contexts_abort_without_partial_backfill(
    isolated_postgres_database,
) -> None:
    config = Config(str(MIGRATIONS))
    command.downgrade(config, "b2c3d4e5f6a7")
    url = make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE desktop_workspaces CASCADE"))
            connection.execute(text("TRUNCATE checkpoints CASCADE"))
            connection.execute(
                text(
                    "INSERT INTO desktop_workspaces (workspace_id, path, display_name) VALUES "
                    "('validation-w1', '/validation-workspace-one', 'One'), "
                    "('validation-w2', '/validation-workspace-two', 'Two')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO desktop_threads "
                    "(task_id, workspace_id, thread_id, title, ui_state) VALUES "
                    "('validation-parent', 'validation-w1', 'validation-thread-parent', "
                    "'Parent', '{}'::jsonb), "
                    "('validation-child', 'validation-w2', 'validation-thread-child', "
                    "'Child', '{}'::jsonb)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO checkpoints "
                    "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) VALUES "
                    "('validation-thread-parent', '', 'validation-checkpoint-parent', "
                    "'{}'::jsonb, '{}'::jsonb)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO desktop_context_revisions "
                    "(revision_id, context_id, generation, execution_thread_id, checkpoint_ns, "
                    "checkpoint_id, payload_mode, authored_messages, execution_messages, "
                    "repair_manifest, issues, initial_message_ids, content_hash, projection_status, "
                    "origin_kind) VALUES "
                    "('validation-revision-parent', 'validation-parent', 1, "
                    "'validation-thread-parent', '', 'validation-checkpoint-parent', "
                    "'checkpoint', '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, "
                    "'[]'::jsonb, repeat('a', 64), 'valid', 'migration')"
                )
            )
            connection.execute(
                text(
                    "UPDATE desktop_threads SET current_revision_id = 'validation-revision-parent' "
                    "WHERE task_id = 'validation-child'"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE desktop_context_sources "
                    "DROP CONSTRAINT uq_desktop_context_source_position"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO desktop_context_sources "
                    "(source_id, context_id, parent_context_id, source_checkpoint_id, position) VALUES "
                    "('validation-bad-source-one', 'validation-child', 'validation-parent', "
                    "'validation-missing-checkpoint', 0), "
                    "('validation-bad-source-two', 'validation-child', 'validation-parent', "
                    "'validation-checkpoint-parent', 0)"
                )
            )

        with pytest.raises(RuntimeError) as exc_info:
            command.upgrade(config, "head")
        message = str(exc_info.value)
        for category in (
            "workspace_mismatch",
            "checkpoint_mismatch",
            "missing_definition",
            "duplicate_source_position",
            "invalid_current_pointer",
        ):
            assert category in message

        with engine.connect() as connection:
            migration_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM desktop_context_revisions "
                    "WHERE origin_kind = 'migration' "
                    "AND revision_id <> 'validation-revision-parent'"
                )
            ).scalar_one()
            alembic_revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert migration_count == 0
            assert alembic_revision == "b2c3d4e5f6a7"

    finally:
        command.downgrade(config, "b2c3d4e5f6a7")
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE desktop_workspaces CASCADE"))
            connection.execute(text("TRUNCATE checkpoints CASCADE"))
            connection.execute(
                text(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'uq_desktop_context_source_position') THEN "
                    "ALTER TABLE desktop_context_sources ADD CONSTRAINT "
                    "uq_desktop_context_source_position UNIQUE (context_id, position); "
                    "END IF; END $$"
                )
            )
        command.upgrade(config, "head")
        engine.dispose()
