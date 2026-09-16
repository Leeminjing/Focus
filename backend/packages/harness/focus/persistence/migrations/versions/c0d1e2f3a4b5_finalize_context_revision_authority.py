r"""本文件对外提供 Context revision 最终权威切换的可逆结构迁移。

输入为已经完成 backfill、parity 和运行时切换的数据库；输出为不再包含 identity-level definition/source
表的 schema。具体工作流为先验证旧 definition 与来源均存在 revision-backed 对应事实，再删除旧表；
downgrade 仅重建空兼容结构，历史数据恢复必须使用升级前备份。示例：`alembic upgrade c0d1e2f3a4b5`。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, Sequence[str], None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM desktop_context_definitions d
            JOIN desktop_threads t ON t.task_id = d.context_id
            LEFT JOIN desktop_context_revisions r
              ON r.revision_id = t.current_revision_id
             AND r.context_id = t.task_id
            WHERE r.revision_id IS NULL
          ) THEN
            RAISE EXCEPTION 'Context definition cleanup blocked: missing current revision';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM desktop_context_sources s
            WHERE NOT EXISTS (
              SELECT 1
              FROM desktop_context_revision_sources rs
              JOIN desktop_context_revisions target
                ON target.revision_id = rs.target_revision_id
              WHERE target.context_id = s.context_id
                AND rs.source_context_id = s.parent_context_id
                AND rs.source_checkpoint_id = s.source_checkpoint_id
                AND rs.position = s.position
            )
          ) THEN
            RAISE EXCEPTION 'Context source cleanup blocked: missing revision source';
          END IF;
        END
        $$
        """
    )
    op.drop_table("desktop_context_sources")
    op.drop_table("desktop_context_definitions")


def downgrade() -> None:
    op.create_table(
        "desktop_context_definitions",
        sa.Column(
            "context_id",
            sa.String(32),
            sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("authored_messages", postgresql.JSONB(), nullable=False),
        sa.Column("execution_messages", postgresql.JSONB(), nullable=False),
        sa.Column("repair_manifest", postgresql.JSONB(), nullable=False),
        sa.Column("issues", postgresql.JSONB(), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("projection_hash", sa.String(64), nullable=False),
        sa.Column("projection_status", sa.String(32), nullable=False),
        sa.Column("initial_message_ids", postgresql.JSONB(), nullable=False),
        sa.Column("initial_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decided_definition_hash", sa.String(64), nullable=True),
        sa.Column("decided_projection_hash", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_desktop_context_definitions_status",
        "desktop_context_definitions",
        ["projection_status"],
    )
    op.create_table(
        "desktop_context_sources",
        sa.Column("source_id", sa.String(32), primary_key=True),
        sa.Column(
            "context_id",
            sa.String(32),
            sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_context_id",
            sa.String(32),
            sa.ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_checkpoint_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "context_id",
            "position",
            name="uq_desktop_context_source_position",
        ),
    )
    op.create_index(
        "ix_desktop_context_sources_context_id",
        "desktop_context_sources",
        ["context_id"],
    )
    op.create_index(
        "ix_desktop_context_sources_parent_context_id",
        "desktop_context_sources",
        ["parent_context_id"],
    )
