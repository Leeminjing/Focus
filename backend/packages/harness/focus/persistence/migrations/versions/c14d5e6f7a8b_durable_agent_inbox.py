"""本文件对外提供 inbox 投递 receipt 的扩展式迁移。

输入为 V2 history schema；输出为绑定消息、Run 和精确 execution checkpoint 的唯一投递记录。
具体工作流为新增有来源外键的 receipt 表，不改写既有消息或已读标记。示例：alembic upgrade head。
"""

from alembic import op
import sqlalchemy as sa

revision = "c14d5e6f7a8b"
down_revision = "c03b4c5d6e7f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_message_deliveries",
                    sa.Column("delivery_id", sa.String(64), primary_key=True),
                    sa.Column("message_id", sa.String(32), sa.ForeignKey("agent_messages.message_id", ondelete="CASCADE"), nullable=False),
                    sa.Column("execution_thread_id", sa.Text(), nullable=False),
                    sa.Column("checkpoint_ns", sa.Text(), nullable=False),
                    sa.Column("checkpoint_id", sa.Text(), nullable=False),
                    sa.Column("run_id", sa.String(32), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
                    sa.UniqueConstraint("message_id", "execution_thread_id", "checkpoint_ns", "checkpoint_id", name="uq_inbox_checkpoint_delivery"))


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM agent_message_deliveries)")):
        raise RuntimeError("存在投递事实，不能删除 receipt；停止新 admission 后保留兼容 reader")
    op.drop_table("agent_message_deliveries")
