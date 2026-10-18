"""普通会话运行中补充和下一轮队列。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f47e8f901234"
down_revision = "e36d7e8f9012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("conversation_inputs",
        sa.Column("input_id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("target_run_id", sa.String(32), sa.ForeignKey("desktop_runs.run_id", ondelete="SET NULL")),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("desktop_runs.run_id", ondelete="SET NULL")),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("checkpoint_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
        sa.CheckConstraint("mode IN ('steer', 'queue')", name="ck_conversation_input_mode"),
        sa.CheckConstraint("status IN ('accepted','queued','delivered','started','not_delivered','blocked','withdrawn')",
                           name="ck_conversation_input_status"))
    op.create_index("ix_conversation_input_task_order", "conversation_inputs", ["task_id", "created_at", "input_id"])


def downgrade():
    op.drop_table("conversation_inputs")
