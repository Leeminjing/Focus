"""本文件对外提供模型审计与工具结果恢复 ledger 的扩展式数据库迁移。

输入为 durable inbox schema；输出为来源绑定的模型审计与唯一工具执行记录，均不属于任务语义历史。
具体工作流为增加审计表，完整结果用 JSONB 保存，存在事实时拒绝 destructive downgrade。
示例：alembic upgrade head。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "c25e6f7a8b9c"
down_revision = "c14d5e6f7a8b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("model_attempt_audits",
        sa.Column("attempt_id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("desktop_runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("execution_thread_id", sa.Text(), nullable=False), sa.Column("checkpoint_ns", sa.Text(), nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False), sa.Column("source_manifest", JSONB(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False), sa.Column("audit", JSONB(), nullable=True), sa.Column("usage", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table("tool_execution_attempts",
        sa.Column("attempt_id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("desktop_runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False), sa.Column("call_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False), sa.Column("result", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))


def downgrade():
    for table in ("tool_execution_attempts", "model_attempt_audits"):
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")):
            raise RuntimeError("存在模型/工具尝试事实，不能删除恢复与审计来源")
    op.drop_table("tool_execution_attempts")
    op.drop_table("model_attempt_audits")
