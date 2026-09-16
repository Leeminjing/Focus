r"""本文件对外提供持续多 Context 策展持久化结构的可逆数据库迁移。

输入为 Context revision 回填后的 Alembic revision `c3d4e5f6a7b8`；输出为 Program、订阅、Lane、
Portfolio revision、candidate 与 attempt 表。具体工作流为先创建 Program 和稳定职责对象，再建立
Portfolio 聚合与 current pointer，partial unique index 保证 managed Context 单发布者。
示例：`alembic upgrade d5e6f7a8b9c0`。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "curation_programs",
        sa.Column("program_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("patrol_id", sa.String(length=32), nullable=True),
        sa.Column("control_state", sa.String(length=16), server_default="following", nullable=False),
        sa.Column(
            "policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("current_portfolio_revision_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "control_state IN ('following', 'paused', 'stopped')",
            name="ck_curation_program_control_state",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_curation_program_revision_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["desktop_workspaces.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["patrol_id"],
            ["patrol_agents.agent_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("program_id"),
    )
    op.create_index(
        "ix_curation_programs_workspace_id",
        "curation_programs",
        ["workspace_id"],
    )
    op.create_index("ix_curation_programs_patrol_id", "curation_programs", ["patrol_id"])

    op.create_table(
        "curation_source_subscriptions",
        sa.Column("subscription_id", sa.String(length=32), nullable=False),
        sa.Column("program_id", sa.String(length=32), nullable=False),
        sa.Column("source_context_id", sa.String(length=32), nullable=False),
        sa.Column("source_role", sa.String(length=64), nullable=False),
        sa.Column(
            "selection_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint("position >= 0", name="ck_curation_subscription_position"),
        sa.ForeignKeyConstraint(
            ["program_id"], ["curation_programs.program_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_context_id"], ["desktop_threads.task_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("subscription_id"),
        sa.UniqueConstraint(
            "program_id", "source_context_id", name="uq_curation_subscription_source"
        ),
        sa.UniqueConstraint(
            "program_id", "position", name="uq_curation_subscription_position"
        ),
    )
    op.create_index(
        "ix_curation_source_subscriptions_program_id",
        "curation_source_subscriptions",
        ["program_id"],
    )
    op.create_index(
        "ix_curation_source_subscriptions_source_context_id",
        "curation_source_subscriptions",
        ["source_context_id"],
    )

    op.create_table(
        "curation_lanes",
        sa.Column("lane_id", sa.String(length=32), nullable=False),
        sa.Column("program_id", sa.String(length=32), nullable=False),
        sa.Column("managed_context_id", sa.String(length=32), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("normalized_purpose", sa.String(length=160), nullable=False),
        sa.Column(
            "lane_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("lifecycle", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("publisher_epoch", sa.Integer(), server_default="1", nullable=False),
        sa.Column("current_source_frontier_hash", sa.String(length=64), nullable=True),
        sa.Column("current_semantic_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "lifecycle IN ('active', 'paused', 'retired')",
            name="ck_curation_lane_lifecycle",
        ),
        sa.CheckConstraint(
            "publisher_epoch > 0",
            name="ck_curation_lane_publisher_epoch_positive",
        ),
        sa.ForeignKeyConstraint(
            ["program_id"], ["curation_programs.program_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["managed_context_id"], ["desktop_threads.task_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("lane_id"),
        sa.UniqueConstraint(
            "program_id", "normalized_purpose", name="uq_curation_lane_purpose"
        ),
    )
    op.create_index("ix_curation_lanes_program_id", "curation_lanes", ["program_id"])
    op.create_index(
        "ix_curation_lanes_managed_context_id",
        "curation_lanes",
        ["managed_context_id"],
    )
    op.create_index(
        "uq_curation_lane_managed_publisher",
        "curation_lanes",
        ["managed_context_id"],
        unique=True,
        postgresql_where=sa.text(
            "managed_context_id IS NOT NULL AND lifecycle <> 'retired'"
        ),
    )

    op.create_table(
        "curation_portfolio_revisions",
        sa.Column("portfolio_revision_id", sa.String(length=32), nullable=False),
        sa.Column("program_id", sa.String(length=32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "source_frontier",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("frontier_hash", sa.String(length=64), nullable=False),
        sa.Column("base_program_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="observed", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "base_program_revision >= 0",
            name="ck_curation_portfolio_base_revision_nonnegative",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_curation_portfolio_generation_positive",
        ),
        sa.CheckConstraint(
            "status IN ('observed', 'deciding', 'waiting_workers', 'preparing', 'ready', "
            "'publishing', 'published', 'superseded', 'degraded', 'waiting_user', 'error')",
            name="ck_curation_portfolio_status",
        ),
        sa.ForeignKeyConstraint(
            ["program_id"], ["curation_programs.program_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("portfolio_revision_id"),
        sa.UniqueConstraint(
            "program_id", "generation", name="uq_curation_portfolio_generation"
        ),
    )
    op.create_index(
        "ix_curation_portfolio_revisions_program_id",
        "curation_portfolio_revisions",
        ["program_id"],
    )
    op.create_foreign_key(
        "fk_curation_program_current_portfolio",
        "curation_programs",
        "curation_portfolio_revisions",
        ["current_portfolio_revision_id"],
        ["portfolio_revision_id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "curation_portfolio_lane_candidates",
        sa.Column("candidate_id", sa.String(length=32), nullable=False),
        sa.Column("portfolio_revision_id", sa.String(length=32), nullable=False),
        sa.Column("lane_id", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("base_context_revision_id", sa.String(length=32), nullable=True),
        sa.Column("candidate_context_revision_id", sa.String(length=32), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column(
            "source_allocation",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("source_frontier_hash", sa.String(length=64), nullable=True),
        sa.Column("semantic_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "action IN ('create', 'update', 'keep', 'pause', 'retire')",
            name="ck_curation_candidate_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'preparing', 'ready', 'published', 'unchanged', "
            "'error', 'superseded')",
            name="ck_curation_candidate_status",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_revision_id"],
            ["curation_portfolio_revisions.portfolio_revision_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lane_id"], ["curation_lanes.lane_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["base_context_revision_id"],
            ["desktop_context_revisions.revision_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_context_revision_id"],
            ["desktop_context_revisions.revision_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("candidate_id"),
        sa.UniqueConstraint(
            "portfolio_revision_id", "lane_id", name="uq_curation_candidate_lane"
        ),
    )
    op.create_index(
        "ix_curation_portfolio_lane_candidates_portfolio_revision_id",
        "curation_portfolio_lane_candidates",
        ["portfolio_revision_id"],
    )
    op.create_index(
        "ix_curation_portfolio_lane_candidates_lane_id",
        "curation_portfolio_lane_candidates",
        ["lane_id"],
    )

    op.create_table(
        "curation_attempts",
        sa.Column("attempt_id", sa.String(length=32), nullable=False),
        sa.Column("candidate_id", sa.String(length=32), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_kind", sa.String(length=24), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column(
            "input_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "raw_output",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "parsed_output",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_curation_attempt_number_positive",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'success', 'error', 'interrupted', 'superseded')",
            name="ck_curation_attempt_status",
        ),
        sa.CheckConstraint(
            "worker_kind IN ('lane_curator', 'legacy_curator')",
            name="ck_curation_attempt_worker_kind",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["curation_portfolio_lane_candidates.candidate_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint(
            "candidate_id", "attempt_number", name="uq_curation_attempt_number"
        ),
    )
    op.create_index(
        "ix_curation_attempts_candidate_id",
        "curation_attempts",
        ["candidate_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_curation_attempts_candidate_id", table_name="curation_attempts")
    op.drop_table("curation_attempts")
    op.drop_index(
        "ix_curation_portfolio_lane_candidates_lane_id",
        table_name="curation_portfolio_lane_candidates",
    )
    op.drop_index(
        "ix_curation_portfolio_lane_candidates_portfolio_revision_id",
        table_name="curation_portfolio_lane_candidates",
    )
    op.drop_table("curation_portfolio_lane_candidates")
    op.drop_constraint(
        "fk_curation_program_current_portfolio",
        "curation_programs",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_curation_portfolio_revisions_program_id",
        table_name="curation_portfolio_revisions",
    )
    op.drop_table("curation_portfolio_revisions")
    op.drop_index(
        "uq_curation_lane_managed_publisher",
        table_name="curation_lanes",
    )
    op.drop_index("ix_curation_lanes_managed_context_id", table_name="curation_lanes")
    op.drop_index("ix_curation_lanes_program_id", table_name="curation_lanes")
    op.drop_table("curation_lanes")
    op.drop_index(
        "ix_curation_source_subscriptions_source_context_id",
        table_name="curation_source_subscriptions",
    )
    op.drop_index(
        "ix_curation_source_subscriptions_program_id",
        table_name="curation_source_subscriptions",
    )
    op.drop_table("curation_source_subscriptions")
    op.drop_index("ix_curation_programs_patrol_id", table_name="curation_programs")
    op.drop_index("ix_curation_programs_workspace_id", table_name="curation_programs")
    op.drop_table("curation_programs")
