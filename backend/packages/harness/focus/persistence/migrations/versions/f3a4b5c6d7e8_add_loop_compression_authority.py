"""本迁移对外提供 Patrol 自主压缩 policy、candidate 与 resolution 持久化结构。

输入为现有 Agent Loop schema；输出为默认不授权旧 grant 的 compression_policy 列和具唯一提交约束
的两张事实表。具体工作流为 upgrade 仅做 additive 变更并建立外键、状态约束、部分唯一索引，
downgrade 按依赖反序移除。示例：`alembic upgrade head` 后旧 grant 的 policy 仍为 `{}`。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f3a4b5c6d7e8"
down_revision: str | Sequence[str] | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE loop_delegation_grants "
        "ADD COLUMN IF NOT EXISTS compression_policy JSONB DEFAULT '{}'::jsonb NOT NULL"
    )
    op.create_table(
        "loop_compression_candidates",
        sa.Column("candidate_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("pending_decision_id", sa.String(length=32), nullable=False),
        sa.Column("round_id", sa.String(length=32), nullable=False),
        sa.Column("context_id", sa.String(length=32), nullable=False),
        sa.Column("base_context_revision_id", sa.String(length=32), nullable=False),
        sa.Column("base_checkpoint_id", sa.Text(), nullable=False),
        sa.Column("frontier_hash", sa.String(length=64), nullable=False),
        sa.Column("authority_revision", sa.Integer(), nullable=False),
        sa.Column("goal_revision", sa.Integer(), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("normalized_ranges", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("replacement_hash", sa.String(length=64), nullable=False),
        sa.Column("candidate_hash", sa.String(length=64), nullable=False),
        sa.Column("before_tokens", sa.Integer(), nullable=False),
        sa.Column("after_tokens", sa.Integer(), nullable=False),
        sa.Column("protection_evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="prepared", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('prepared','accepted','rejected','superseded','expired')",
            name="ck_loop_compression_candidate_status",
        ),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pending_decision_id"], ["loop_pending_decisions.pending_decision_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["round_id"], ["loop_rounds.round_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["context_id"], ["desktop_threads.task_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["base_context_revision_id"], ["desktop_context_revisions.revision_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("candidate_id"),
        sa.UniqueConstraint("candidate_hash", name="uq_loop_compression_candidate_hash"),
    )
    op.create_index("ix_loop_compression_candidate_loop_created", "loop_compression_candidates", ["loop_id", "created_at"])
    op.create_index(
        "uq_loop_compression_live_candidate",
        "loop_compression_candidates",
        ["pending_decision_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('prepared','accepted')"),
    )
    op.create_table(
        "loop_compression_resolutions",
        sa.Column("resolution_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("pending_decision_id", sa.String(length=32), nullable=False),
        sa.Column("candidate_id", sa.String(length=32), nullable=False),
        sa.Column("decision_id", sa.String(length=32), nullable=False),
        sa.Column("action_id", sa.String(length=32), nullable=False),
        sa.Column("round_id", sa.String(length=32), nullable=False),
        sa.Column("grant_id", sa.String(length=32), nullable=False),
        sa.Column("grant_revision", sa.Integer(), nullable=False),
        sa.Column("goal_revision", sa.Integer(), nullable=False),
        sa.Column("resume_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="committed", nullable=False),
        sa.Column("resume_run_id", sa.String(length=32), nullable=True),
        sa.Column("result_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("result_context_revision_id", sa.String(length=32), nullable=True),
        sa.Column("actual_before_tokens", sa.Integer(), nullable=True),
        sa.Column("actual_after_tokens", sa.Integer(), nullable=True),
        sa.Column("error_evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_token", sa.String(length=32), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('committed','resuming','applied','superseded','failed')",
            name="ck_loop_compression_resolution_status",
        ),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pending_decision_id"], ["loop_pending_decisions.pending_decision_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["loop_compression_candidates.candidate_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decision_id"], ["loop_decisions.decision_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["action_id"], ["loop_actions.action_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["round_id"], ["loop_rounds.round_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["grant_id"], ["loop_delegation_grants.grant_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resume_run_id"], ["desktop_runs.run_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["result_context_revision_id"], ["desktop_context_revisions.revision_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("resolution_id"),
        sa.UniqueConstraint("pending_decision_id", name="uq_loop_compression_resolution_pending"),
        sa.UniqueConstraint("candidate_id", name="uq_loop_compression_resolution_candidate"),
        sa.UniqueConstraint("idempotency_key", name="uq_loop_compression_resolution_idempotency"),
    )
    op.create_index(
        "ix_loop_compression_resolution_status_lease",
        "loop_compression_resolutions",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_table("loop_compression_resolutions")
    op.drop_table("loop_compression_candidates")
    op.execute("ALTER TABLE loop_delegation_grants DROP COLUMN IF EXISTS compression_policy")
