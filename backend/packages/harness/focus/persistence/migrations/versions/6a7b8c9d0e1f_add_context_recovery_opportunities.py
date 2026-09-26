r"""本迁移对外提供持久化单来源 Context recovery opportunity 表。

输入为已包含 Agent Loop、Context revision、Loop decision 与 semantic derivation 的数据库；输出为绑定 frontier、goal、workspace、
authority、grant、compiler、expiry 与消费结果的恢复机会存储。具体工作流为追加当前状态行和唯一来源约束，发布事务可锁定并原子消费；
downgrade 只移除该表，不改写 Context 或 Portfolio 历史。示例：`alembic upgrade 6a7b8c9d0e1f`。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "6a7b8c9d0e1f"
down_revision = "5f6a7b8c9d0e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loop_context_recovery_opportunities",
        sa.Column("opportunity_id", sa.String(length=64), primary_key=True),
        sa.Column("loop_id", sa.String(length=32), sa.ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False),
        sa.Column("round_id", sa.String(length=32), sa.ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_context_id", sa.String(length=32), sa.ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_revision_id", sa.String(length=32), sa.ForeignKey("desktop_context_revisions.revision_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_frontier_hash", sa.String(length=64), nullable=False),
        sa.Column("goal_revision", sa.Integer(), nullable=False),
        sa.Column("workspace_revision", sa.Integer(), nullable=False),
        sa.Column("authority_revision", sa.Integer(), nullable=False),
        sa.Column("grant_id", sa.String(length=32), nullable=False),
        sa.Column("grant_revision", sa.Integer(), nullable=False),
        sa.Column("source_run_id", sa.String(length=32), nullable=False),
        sa.Column("compiler_version", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("safe_summary", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("consumed_by_decision_id", sa.String(length=32), sa.ForeignKey("loop_decisions.decision_id", ondelete="SET NULL"), nullable=True),
        sa.Column("result", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','consumed','stale','blocked')",
            name="ck_loop_context_recovery_status",
        ),
        sa.UniqueConstraint(
            "loop_id",
            "source_revision_id",
            "source_run_id",
            "compiler_version",
            name="uq_loop_context_recovery_source",
        ),
    )
    op.create_index("ix_loop_context_recovery_loop_id", "loop_context_recovery_opportunities", ["loop_id"])
    op.create_index(
        "ix_loop_context_recovery_round_status",
        "loop_context_recovery_opportunities",
        ["round_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_loop_context_recovery_round_status", table_name="loop_context_recovery_opportunities")
    op.drop_index("ix_loop_context_recovery_loop_id", table_name="loop_context_recovery_opportunities")
    op.drop_table("loop_context_recovery_opportunities")
