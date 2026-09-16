r"""本文件对外提供 legacy Context generation-one revision 回填的 PostgreSQL 等价性验证。

输入为含 root、derived、compressed 与 managed 形态的旧 schema fixtures；输出为 revision、checkpoint 和来源保持断言。
具体工作流为降至结构迁移、写入旧数据、升级回填，再逐 Context 对比定义、执行身份和血缘。
示例：`python -m pytest backend/tests/test_context_revision_backfill.py -q`。
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
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


def test_context_revision_backfill_preserves_legacy_state(isolated_postgres_database) -> None:
    config = Config(str(MIGRATIONS))
    command.downgrade(config, "b2c3d4e5f6a7")
    url = make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO desktop_workspaces (workspace_id, path, display_name) "
                    "VALUES ('w1', '/workspace', 'Workspace')"
                )
            )
            for context_id, thread_id, title in (
                ("root", "thread-root", "Root"),
                ("derived", "thread-derived", "Derived"),
                ("compressed", "thread-compressed", "Compressed"),
                ("managed", "thread-managed", "Managed"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO desktop_threads "
                        "(task_id, workspace_id, thread_id, title, ui_state) "
                        "VALUES (:context_id, 'w1', :thread_id, :title, '{}'::jsonb)"
                    ),
                    {"context_id": context_id, "thread_id": thread_id, "title": title},
                )
                connection.execute(
                    text(
                        "INSERT INTO checkpoints "
                        "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                        "VALUES (:thread_id, '', :checkpoint_id, '{}'::jsonb, '{}'::jsonb)"
                    ),
                    {"thread_id": thread_id, "checkpoint_id": f"checkpoint-{context_id}"},
                )

            for context_id, content, status in (
                ("derived", "derived context", "valid"),
                ("compressed", "compressed summary", "repaired"),
                ("managed", "managed curation", "valid"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO desktop_context_definitions "
                        "(context_id, authored_messages, execution_messages, repair_manifest, issues, "
                        "definition_hash, projection_hash, projection_status, initial_message_ids, "
                        "initial_checkpoint_id) VALUES "
                        "(:context_id, jsonb_build_array(jsonb_build_object(" 
                        "'role','human','content',CAST(:content AS text))), "
                        "jsonb_build_array(jsonb_build_object(" 
                        "'role','human','content',CAST(:content AS text))), "
                        "'[]'::jsonb, '[]'::jsonb, :definition_hash, :projection_hash, :status, "
                        "jsonb_build_array(CAST(:message_id AS text)), :checkpoint_id)"
                    ),
                    {
                        "context_id": context_id,
                        "content": content,
                        "definition_hash": f"definition-{context_id}",
                        "projection_hash": f"projection-{context_id}",
                        "status": status,
                        "message_id": f"message-{context_id}",
                        "checkpoint_id": f"checkpoint-{context_id}",
                    },
                )

            for source_id, child, parent, position in (
                ("source-derived", "derived", "root", 0),
                ("source-managed", "managed", "root", 0),
                ("source-managed-derived", "managed", "derived", 1),
            ):
                connection.execute(
                    text(
                        "INSERT INTO desktop_context_sources "
                        "(source_id, context_id, parent_context_id, source_checkpoint_id, position) "
                        "VALUES (:source_id, :child, :parent, :checkpoint_id, :position)"
                    ),
                    {
                        "source_id": source_id,
                        "child": child,
                        "parent": parent,
                        "checkpoint_id": f"checkpoint-{parent}",
                        "position": position,
                    },
                )

        command.upgrade(config, "b0c1d2e3f4a5")

        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT t.task_id, t.thread_id, t.current_revision_id, r.generation, "
                    "r.execution_thread_id, r.checkpoint_id, r.payload_mode, r.authored_messages, "
                    "r.projection_status, r.origin_kind FROM desktop_threads t "
                    "JOIN desktop_context_revisions r ON r.revision_id = t.current_revision_id "
                    "WHERE t.task_id IN ('root', 'derived', 'compressed', 'managed') "
                    "ORDER BY t.task_id"
                )
            ).mappings().all()
            assert len(rows) == 4
            by_context = {row["task_id"]: row for row in rows}
            for context_id, row in by_context.items():
                assert row["generation"] == 1
                assert row["execution_thread_id"] == row["thread_id"]
                assert row["checkpoint_id"] == f"checkpoint-{context_id}"
                assert row["origin_kind"] == "migration"
            assert by_context["root"]["payload_mode"] == "checkpoint"
            assert by_context["root"]["authored_messages"] == []
            assert by_context["compressed"]["payload_mode"] == "definition"
            assert by_context["compressed"]["projection_status"] == "repaired"
            assert by_context["managed"]["authored_messages"][0]["content"] == "managed curation"
            first_revision_ids = {
                context_id: row["current_revision_id"] for context_id, row in by_context.items()
            }

            sources = connection.execute(
                text(
                    "SELECT target.context_id AS child, source.context_id AS parent, "
                    "edge.source_checkpoint_id, edge.position "
                    "FROM desktop_context_revision_sources edge "
                    "JOIN desktop_context_revisions target ON target.revision_id = edge.target_revision_id "
                    "JOIN desktop_context_revisions source ON source.revision_id = edge.source_revision_id "
                    "WHERE target.context_id IN ('root', 'derived', 'compressed', 'managed') "
                    "ORDER BY child, position"
                )
            ).mappings().all()
            assert [dict(row) for row in sources] == [
                {
                    "child": "derived",
                    "parent": "root",
                    "source_checkpoint_id": "checkpoint-root",
                    "position": 0,
                },
                {
                    "child": "managed",
                    "parent": "root",
                    "source_checkpoint_id": "checkpoint-root",
                    "position": 0,
                },
                {
                    "child": "managed",
                    "parent": "derived",
                    "source_checkpoint_id": "checkpoint-derived",
                    "position": 1,
                },
            ]

        command.downgrade(config, "b2c3d4e5f6a7")
        with engine.connect() as connection:
            legacy_counts = {
                "desktop_threads": connection.execute(text("SELECT COUNT(*) FROM desktop_threads WHERE task_id IN ('root', 'derived', 'compressed', 'managed')")).scalar_one(),
                "desktop_context_definitions": connection.execute(text("SELECT COUNT(*) FROM desktop_context_definitions WHERE context_id IN ('root', 'derived', 'compressed', 'managed')")).scalar_one(),
                "desktop_context_sources": connection.execute(text("SELECT COUNT(*) FROM desktop_context_sources WHERE context_id IN ('root', 'derived', 'compressed', 'managed')")).scalar_one(),
                "checkpoints": connection.execute(text("SELECT COUNT(*) FROM checkpoints WHERE thread_id IN ('thread-root', 'thread-derived', 'thread-compressed', 'thread-managed')")).scalar_one(),
            }
            assert legacy_counts == {
                "desktop_threads": 4,
                "desktop_context_definitions": 3,
                "desktop_context_sources": 3,
                "checkpoints": 4,
            }
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM desktop_context_revisions "
                    "WHERE context_id IN ('root', 'derived', 'compressed', 'managed')"
                )
            ).scalar_one() == 0
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM desktop_threads "
                    "WHERE task_id IN ('root', 'derived', 'compressed', 'managed') "
                    "AND current_revision_id IS NOT NULL"
                )
            ).scalar_one() == 0

        command.upgrade(config, "head")
        with engine.connect() as connection:
            second_revision_ids = dict(
                connection.execute(
                    text(
                        "SELECT task_id, current_revision_id FROM desktop_threads "
                        "WHERE task_id IN ('root', 'derived', 'compressed', 'managed') "
                        "ORDER BY task_id"
                    )
                ).all()
            )
            assert second_revision_ids == first_revision_ids
            assert connection.execute(
                text("SELECT COUNT(*) FROM desktop_context_revision_sources edge JOIN desktop_context_revisions target ON target.revision_id = edge.target_revision_id WHERE target.context_id IN ('root', 'derived', 'compressed', 'managed')")
            ).scalar_one() == 3
    finally:
        command.upgrade(config, "head")
        engine.dispose()
