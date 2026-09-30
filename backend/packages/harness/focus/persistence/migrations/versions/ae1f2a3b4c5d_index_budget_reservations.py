"""本文件对外提供索引模型预算共享预留的 additive 迁移。

输入为 segment record 版数据库；输出为按 Loop 和 build 保存授权版本、预留与结算的持久表。
工作流为新增预留表和非负约束；降级只有在全部预留已结算时才能删除表，避免释放未知已发送工作。
示例：alembic upgrade head；未结算预留会显式阻止 downgrade，Context／Progress 表不变。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "ae1f2a3b4c5d"
down_revision = "9d0e1f2a3b4c"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "loop_index_budget_reservations",
        sa.Column("reservation_id", sa.String(32), primary_key=True),
        sa.Column(
            "loop_id",
            sa.String(32),
            sa.ForeignKey("agent_loops.loop_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("grant_revision", sa.Integer(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("actual_usage", JSONB, nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "grant_revision > 0 AND model_calls >= 0 AND input_tokens >= 0 AND output_tokens >= 0",
            name="ck_loop_index_budget_amounts",
        ),
    )
    op.create_index(
        "ix_loop_index_budget_reservations_loop_id",
        "loop_index_budget_reservations",
        ["loop_id"],
    )


def downgrade():
    pending = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM loop_index_budget_reservations WHERE settled_at IS NULL"
        )
    )
    if pending:
        raise RuntimeError("Cannot downgrade with unsettled index budget reservations")
    op.drop_table("loop_index_budget_reservations")
