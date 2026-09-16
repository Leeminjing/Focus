"""本迁移对外提供 Loop 用户意图的持久化表。

输入为已存在的 Agent Loop、Context 与控制版本；输出为可审计的 Context/Portfolio 级用户意见。
具体工作流为 upgrade 创建约束、外键与待观察索引，downgrade 完整移除该表。
示例：`alembic upgrade head` 后可写入 `loop_user_intents`。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e2f3a4b5c6d7"
down_revision: str | Sequence[str] | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "loop_user_intents",
        sa.Column("intent_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("target_context_id", sa.String(length=32), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("goal_revision", sa.Integer(), nullable=False),
        sa.Column("authority_revision", sa.Integer(), nullable=False),
        sa.Column("observed_round_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("addressed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("scope IN ('context','portfolio')", name="ck_loop_user_intent_scope"),
        sa.CheckConstraint("status IN ('pending','observed','addressed','superseded')", name="ck_loop_user_intent_status"),
        sa.CheckConstraint(
            "(scope = 'context' AND target_context_id IS NOT NULL) OR "
            "(scope = 'portfolio' AND target_context_id IS NULL)",
            name="ck_loop_user_intent_target",
        ),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_context_id"], ["desktop_threads.task_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("intent_id"),
    )
    op.create_index("ix_loop_user_intents_loop_id", "loop_user_intents", ["loop_id"])
    op.create_index("ix_loop_user_intents_target_context_id", "loop_user_intents", ["target_context_id"])
    op.create_index("ix_loop_user_intents_observed_round_id", "loop_user_intents", ["observed_round_id"])
    op.create_index("ix_loop_user_intent_pending", "loop_user_intents", ["loop_id", "status", "created_at"])


def downgrade() -> None:
    op.drop_table("loop_user_intents")
