r"""本文件对外提供 Context revision 与版本化来源的可逆数据库迁移。

输入为当前 Alembic head `a1b2c3d4e5f6` 的 Desktop 数据库；输出为 revision/source 表和 current pointer。
具体工作流为先创建不可变历史表，再增加来源边，最后给 Context identity 添加可空当前指针。
示例：`alembic upgrade b2c3d4e5f6a7`。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "desktop_context_revisions",
        sa.Column("revision_id", sa.String(length=32), nullable=False),
        sa.Column("context_id", sa.String(length=32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("execution_thread_id", sa.String(length=128), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=True),
        sa.Column("payload_mode", sa.String(length=16), nullable=False),
        sa.Column(
            "authored_messages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "execution_messages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "repair_manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "issues",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "initial_message_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("definition_hash", sa.String(length=64), nullable=True),
        sa.Column("projection_hash", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("projection_status", sa.String(length=32), nullable=False),
        sa.Column("origin_kind", sa.String(length=32), nullable=False),
        sa.Column("origin_id", sa.String(length=64), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("generation > 0", name="ck_context_revision_generation_positive"),
        sa.CheckConstraint(
            "payload_mode IN ('checkpoint', 'definition')",
            name="ck_context_revision_payload_mode",
        ),
        sa.CheckConstraint(
            "projection_status IN ('preparing', 'valid', 'repaired', 'approved', 'approval_required', "
            "'rejected', 'error', 'deleted')",
            name="ck_context_revision_projection_status",
        ),
        sa.CheckConstraint(
            "origin_kind IN ('root', 'manual_derive', 'definition_update', 'projection_decision', "
            "'compression', 'compression_restore', 'run_settled', 'curation', 'migration')",
            name="ck_context_revision_origin_kind",
        ),
        sa.CheckConstraint(
            "payload_mode <> 'checkpoint' OR checkpoint_id IS NOT NULL",
            name="ck_context_revision_checkpoint_payload",
        ),
        sa.ForeignKeyConstraint(
            ["context_id"],
            ["desktop_threads.task_id"],
            name="fk_context_revision_context",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("revision_id", name="pk_desktop_context_revisions"),
        sa.UniqueConstraint(
            "context_id",
            "generation",
            name="uq_context_revision_generation",
        ),
        sa.UniqueConstraint(
            "execution_thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_context_revision_execution_checkpoint",
        ),
    )
    op.create_index(
        "ix_desktop_context_revisions_context_id",
        "desktop_context_revisions",
        ["context_id"],
    )

    op.create_table(
        "desktop_context_revision_sources",
        sa.Column("source_edge_id", sa.String(length=32), nullable=False),
        sa.Column("target_revision_id", sa.String(length=32), nullable=False),
        sa.Column("source_context_id", sa.String(length=32), nullable=False),
        sa.Column("source_revision_id", sa.String(length=32), nullable=False),
        sa.Column("source_checkpoint_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_context_revision_source_position"),
        sa.CheckConstraint(
            "target_revision_id <> source_revision_id",
            name="ck_context_revision_source_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["target_revision_id"],
            ["desktop_context_revisions.revision_id"],
            name="fk_context_revision_source_target",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_context_id"],
            ["desktop_threads.task_id"],
            name="fk_context_revision_source_context",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_revision_id"],
            ["desktop_context_revisions.revision_id"],
            name="fk_context_revision_source_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("source_edge_id", name="pk_desktop_context_revision_sources"),
        sa.UniqueConstraint(
            "target_revision_id",
            "position",
            name="uq_context_revision_source_position",
        ),
        sa.UniqueConstraint(
            "target_revision_id",
            "source_revision_id",
            name="uq_context_revision_source_revision",
        ),
    )
    op.create_index(
        "ix_context_revision_sources_target",
        "desktop_context_revision_sources",
        ["target_revision_id"],
    )
    op.create_index(
        "ix_context_revision_sources_source_context",
        "desktop_context_revision_sources",
        ["source_context_id"],
    )
    op.create_index(
        "ix_context_revision_sources_source_revision",
        "desktop_context_revision_sources",
        ["source_revision_id"],
    )

    op.add_column(
        "desktop_threads",
        sa.Column("current_revision_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_desktop_thread_current_revision",
        "desktop_threads",
        "desktop_context_revisions",
        ["current_revision_id"],
        ["revision_id"],
        ondelete="SET NULL",
        use_alter=True,
    )
    op.create_index(
        "ix_desktop_threads_current_revision_id",
        "desktop_threads",
        ["current_revision_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_desktop_threads_current_revision_id", table_name="desktop_threads")
    op.drop_constraint(
        "fk_desktop_thread_current_revision",
        "desktop_threads",
        type_="foreignkey",
    )
    op.drop_column("desktop_threads", "current_revision_id")
    op.drop_index(
        "ix_context_revision_sources_source_revision",
        table_name="desktop_context_revision_sources",
    )
    op.drop_index(
        "ix_context_revision_sources_source_context",
        table_name="desktop_context_revision_sources",
    )
    op.drop_index(
        "ix_context_revision_sources_target",
        table_name="desktop_context_revision_sources",
    )
    op.drop_table("desktop_context_revision_sources")
    op.drop_index(
        "ix_desktop_context_revisions_context_id",
        table_name="desktop_context_revisions",
    )
    op.drop_table("desktop_context_revisions")
