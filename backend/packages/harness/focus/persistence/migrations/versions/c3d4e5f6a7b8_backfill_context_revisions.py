r"""本文件对外提供旧 Context 状态到 generation-one revision 的确定性可逆回填。

输入为旧 Desktop Context identity、definition/source 与 LangGraph checkpoint；输出为一对一初始 revision 和来源边。
具体工作流为按 Context 排序冻结旧状态、生成内容寻址身份、复制定义与 checkpoint，再设置 current pointer。
示例：`alembic upgrade c3d4e5f6a7b8`。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


threads = sa.table(
    "desktop_threads",
    sa.column("task_id", sa.String(32)),
    sa.column("workspace_id", sa.String(32)),
    sa.column("thread_id", sa.String(128)),
    sa.column("current_revision_id", sa.String(32)),
)
definitions = sa.table(
    "desktop_context_definitions",
    sa.column("context_id", sa.String(32)),
    sa.column("authored_messages", postgresql.JSONB()),
    sa.column("execution_messages", postgresql.JSONB()),
    sa.column("repair_manifest", postgresql.JSONB()),
    sa.column("issues", postgresql.JSONB()),
    sa.column("definition_hash", sa.String(64)),
    sa.column("projection_hash", sa.String(64)),
    sa.column("projection_status", sa.String(32)),
    sa.column("initial_message_ids", postgresql.JSONB()),
    sa.column("initial_checkpoint_id", sa.Text()),
)
legacy_sources = sa.table(
    "desktop_context_sources",
    sa.column("source_id", sa.String(32)),
    sa.column("context_id", sa.String(32)),
    sa.column("parent_context_id", sa.String(32)),
    sa.column("source_checkpoint_id", sa.Text()),
    sa.column("position", sa.Integer()),
)
checkpoints = sa.table(
    "checkpoints",
    sa.column("thread_id", sa.Text()),
    sa.column("checkpoint_ns", sa.Text()),
    sa.column("checkpoint_id", sa.Text()),
)
revisions = sa.table(
    "desktop_context_revisions",
    sa.column("revision_id", sa.String(32)),
    sa.column("context_id", sa.String(32)),
    sa.column("generation", sa.Integer()),
    sa.column("execution_thread_id", sa.String(128)),
    sa.column("checkpoint_ns", sa.Text()),
    sa.column("checkpoint_id", sa.Text()),
    sa.column("payload_mode", sa.String(16)),
    sa.column("authored_messages", postgresql.JSONB()),
    sa.column("execution_messages", postgresql.JSONB()),
    sa.column("repair_manifest", postgresql.JSONB()),
    sa.column("issues", postgresql.JSONB()),
    sa.column("initial_message_ids", postgresql.JSONB()),
    sa.column("definition_hash", sa.String(64)),
    sa.column("projection_hash", sa.String(64)),
    sa.column("content_hash", sa.String(64)),
    sa.column("projection_status", sa.String(32)),
    sa.column("origin_kind", sa.String(32)),
    sa.column("origin_id", sa.String(64)),
)
revision_sources = sa.table(
    "desktop_context_revision_sources",
    sa.column("source_edge_id", sa.String(32)),
    sa.column("target_revision_id", sa.String(32)),
    sa.column("source_context_id", sa.String(32)),
    sa.column("source_revision_id", sa.String(32)),
    sa.column("source_checkpoint_id", sa.Text()),
    sa.column("position", sa.Integer()),
)


def _stable_id(kind: str, *parts: object) -> str:
    payload = ":".join([kind, *(str(part) for part in parts)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _latest_checkpoint_id(connection, thread_id: str) -> str | None:
    return connection.execute(
        sa.select(checkpoints.c.checkpoint_id)
        .where(
            checkpoints.c.thread_id == thread_id,
            checkpoints.c.checkpoint_ns == "",
        )
        .order_by(checkpoints.c.checkpoint_id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _legacy_rows(connection) -> list[dict[str, Any]]:
    statement = (
        sa.select(
            threads.c.task_id,
            threads.c.workspace_id,
            threads.c.thread_id,
            threads.c.current_revision_id,
            definitions.c.context_id.label("definition_context_id"),
            definitions.c.authored_messages,
            definitions.c.execution_messages,
            definitions.c.repair_manifest,
            definitions.c.issues,
            definitions.c.definition_hash,
            definitions.c.projection_hash,
            definitions.c.projection_status,
            definitions.c.initial_message_ids,
            definitions.c.initial_checkpoint_id,
        )
        .select_from(threads.outerjoin(definitions, definitions.c.context_id == threads.c.task_id))
        .order_by(threads.c.task_id)
    )
    return [dict(row) for row in connection.execute(statement).mappings()]


def _invalid_ids(connection, statement: str) -> list[str]:
    return [str(value) for value in connection.execute(sa.text(statement)).scalars()]


def _require_legacy_integrity(connection) -> None:
    problems = {
        "workspace_mismatch": _invalid_ids(
            connection,
            "SELECT source.source_id FROM desktop_context_sources source "
            "JOIN desktop_threads child ON child.task_id = source.context_id "
            "JOIN desktop_threads parent ON parent.task_id = source.parent_context_id "
            "WHERE child.workspace_id <> parent.workspace_id ORDER BY source.source_id",
        ),
        "checkpoint_mismatch": _invalid_ids(
            connection,
            "SELECT source.source_id FROM desktop_context_sources source "
            "JOIN desktop_threads parent ON parent.task_id = source.parent_context_id "
            "LEFT JOIN checkpoints checkpoint ON checkpoint.thread_id = parent.thread_id "
            "AND checkpoint.checkpoint_ns = '' "
            "AND checkpoint.checkpoint_id = source.source_checkpoint_id "
            "WHERE checkpoint.checkpoint_id IS NULL ORDER BY source.source_id",
        ),
        "missing_definition": _invalid_ids(
            connection,
            "SELECT DISTINCT child.task_id FROM desktop_context_sources source "
            "JOIN desktop_threads child ON child.task_id = source.context_id "
            "LEFT JOIN desktop_context_definitions definition ON definition.context_id = child.task_id "
            "WHERE definition.context_id IS NULL AND child.deleted_at IS NULL ORDER BY child.task_id",
        ),
        "duplicate_source_position": _invalid_ids(
            connection,
            "SELECT context_id || ':' || position::text FROM desktop_context_sources "
            "GROUP BY context_id, position HAVING COUNT(*) > 1 ORDER BY context_id, position",
        ),
        "invalid_current_pointer": _invalid_ids(
            connection,
            "SELECT thread.task_id FROM desktop_threads thread "
            "LEFT JOIN desktop_context_revisions revision "
            "ON revision.revision_id = thread.current_revision_id "
            "WHERE thread.current_revision_id IS NOT NULL "
            "AND (revision.revision_id IS NULL OR revision.context_id <> thread.task_id) "
            "ORDER BY thread.task_id",
        ),
    }
    failures = [f"{kind}={','.join(ids)}" for kind, ids in problems.items() if ids]
    if failures:
        raise RuntimeError("legacy context validation failed: " + "; ".join(failures))


def upgrade() -> None:
    connection = op.get_bind()
    _require_legacy_integrity(connection)
    rows = _legacy_rows(connection)
    revision_ids: dict[str, str] = {}

    for row in rows:
        if row["current_revision_id"] is not None:
            continue
        context_id = row["task_id"]
        revision_id = _stable_id("context-revision", context_id, 1)
        revision_ids[context_id] = revision_id
        checkpoint_id = _latest_checkpoint_id(connection, row["thread_id"])
        if checkpoint_id is None:
            checkpoint_id = row["initial_checkpoint_id"]
        has_definition = row["definition_context_id"] is not None
        authored_messages = row["authored_messages"] if has_definition else []
        execution_messages = row["execution_messages"] if has_definition else []
        repair_manifest = row["repair_manifest"] if has_definition else []
        issues = row["issues"] if has_definition else []
        initial_message_ids = row["initial_message_ids"] if has_definition else []
        projection_status = row["projection_status"] if has_definition else "valid"
        payload_mode = "definition" if has_definition or checkpoint_id is None else "checkpoint"
        payload = {
            "context_id": context_id,
            "generation": 1,
            "execution_thread_id": row["thread_id"],
            "checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
            "payload_mode": payload_mode,
            "authored_messages": authored_messages,
            "execution_messages": execution_messages,
            "repair_manifest": repair_manifest,
            "issues": issues,
            "initial_message_ids": initial_message_ids,
            "definition_hash": row["definition_hash"] if has_definition else None,
            "projection_hash": row["projection_hash"] if has_definition else None,
            "projection_status": projection_status,
        }
        connection.execute(
            revisions.insert().values(
                revision_id=revision_id,
                **payload,
                content_hash=_content_hash(payload),
                origin_kind="migration",
                origin_id=context_id,
            )
        )

    for source in connection.execute(
        sa.select(legacy_sources).order_by(
            legacy_sources.c.context_id,
            legacy_sources.c.position,
        )
    ).mappings():
        target_revision_id = revision_ids.get(source["context_id"])
        source_revision_id = revision_ids.get(source["parent_context_id"])
        if target_revision_id is None or source_revision_id is None:
            continue
        connection.execute(
            revision_sources.insert().values(
                source_edge_id=_stable_id("context-revision-source", source["source_id"]),
                target_revision_id=target_revision_id,
                source_context_id=source["parent_context_id"],
                source_revision_id=source_revision_id,
                source_checkpoint_id=source["source_checkpoint_id"],
                position=source["position"],
            )
        )

    for context_id, revision_id in revision_ids.items():
        connection.execute(
            threads.update()
            .where(
                threads.c.task_id == context_id,
                threads.c.current_revision_id.is_(None),
            )
            .values(current_revision_id=revision_id)
        )


def downgrade() -> None:
    connection = op.get_bind()
    migration_revision_ids = sa.select(revisions.c.revision_id).where(
        revisions.c.origin_kind == "migration"
    )
    connection.execute(
        threads.update()
        .where(threads.c.current_revision_id.in_(migration_revision_ids))
        .values(current_revision_id=None)
    )
    connection.execute(
        revision_sources.delete().where(
            revision_sources.c.target_revision_id.in_(migration_revision_ids)
        )
    )
    connection.execute(revisions.delete().where(revisions.c.origin_kind == "migration"))
