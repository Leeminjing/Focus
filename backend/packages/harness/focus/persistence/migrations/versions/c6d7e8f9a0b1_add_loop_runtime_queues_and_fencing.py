r"""本迁移对外提供 Loop 运行队列元数据和单调 fencing counter。

输入为已存在的 decision、directive、worker request 与 round 表；输出为发布/Context/Curator 的持久重试字段、
可见排队原因和每 round 单调递增的 fencing token。具体工作流为 upgrade 只追加列和 counter 表，downgrade
按相反顺序移除新增结构。示例：`alembic upgrade head`。
"""

from alembic import op
import sqlalchemy as sa


revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loop_coordinator_fences",
        sa.Column("round_id", sa.String(length=32), nullable=False),
        sa.Column("fencing_token", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["round_id"], ["loop_rounds.round_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("round_id"),
    )
    op.execute("ALTER TABLE loop_decisions ADD COLUMN IF NOT EXISTS fencing_token INTEGER DEFAULT 0 NOT NULL")
    op.execute("ALTER TABLE loop_decisions ADD COLUMN IF NOT EXISTS deferred_attempt INTEGER DEFAULT 0 NOT NULL")
    op.execute("ALTER TABLE loop_decisions ADD COLUMN IF NOT EXISTS queued_reason TEXT")
    op.execute("ALTER TABLE loop_directives ADD COLUMN IF NOT EXISTS queued_reason TEXT")
    op.execute("ALTER TABLE loop_directives ADD COLUMN IF NOT EXISTS attempt INTEGER DEFAULT 0 NOT NULL")
    op.execute("ALTER TABLE loop_directives ADD COLUMN IF NOT EXISTS max_attempts INTEGER DEFAULT 3 NOT NULL")
    op.execute("ALTER TABLE loop_worker_requests ADD COLUMN IF NOT EXISTS max_attempts INTEGER DEFAULT 3 NOT NULL")
    op.execute("ALTER TABLE loop_worker_requests ADD COLUMN IF NOT EXISTS retry_identity VARCHAR(160)")


def downgrade() -> None:
    op.execute("ALTER TABLE loop_worker_requests DROP COLUMN IF EXISTS retry_identity")
    op.execute("ALTER TABLE loop_worker_requests DROP COLUMN IF EXISTS max_attempts")
    op.execute("ALTER TABLE loop_directives DROP COLUMN IF EXISTS max_attempts")
    op.execute("ALTER TABLE loop_directives DROP COLUMN IF EXISTS attempt")
    op.execute("ALTER TABLE loop_directives DROP COLUMN IF EXISTS queued_reason")
    op.execute("ALTER TABLE loop_decisions DROP COLUMN IF EXISTS queued_reason")
    op.execute("ALTER TABLE loop_decisions DROP COLUMN IF EXISTS deferred_attempt")
    op.execute("ALTER TABLE loop_decisions DROP COLUMN IF EXISTS fencing_token")
    op.drop_table("loop_coordinator_fences")
