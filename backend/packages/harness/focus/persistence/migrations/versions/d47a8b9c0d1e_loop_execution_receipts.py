"""本文件对外提供 Loop 执行父链与模型消费 receipt 的增量迁移。

输入为 c36f7a8b9c0d 数据库；输出为可空 parent_run_id 与审计内 usage_accounting。
具体工作流为增加列、索引与外键；历史记录保持空来源及空 receipt。有新增审计时拒绝结构降级，软件回退保留增量结构。
示例：alembic upgrade d47a8b9c0d1e，仅在隔离数据库演练。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d47a8b9c0d1e"
down_revision = "c36f7a8b9c0d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("desktop_runs", sa.Column("parent_run_id", sa.String(32), nullable=True))
    op.create_index("ix_desktop_runs_parent_run_id", "desktop_runs", ["parent_run_id"])
    op.create_foreign_key("fk_desktop_runs_parent_run", "desktop_runs", "desktop_runs", ["parent_run_id"], ["run_id"], ondelete="RESTRICT")
    op.add_column("model_attempt_audits", sa.Column("usage_accounting", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))


def downgrade():
    retained = op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM desktop_runs WHERE parent_run_id IS NOT NULL) OR EXISTS (SELECT 1 FROM model_attempt_audits WHERE usage_accounting <> '{}'::jsonb)")).scalar()
    if retained:
        raise RuntimeError("回退会删除执行父链或消费审计；请保留增量 schema 回退软件，并先备份审计")
    op.drop_column("model_attempt_audits", "usage_accounting")
    op.drop_constraint("fk_desktop_runs_parent_run", "desktop_runs", type_="foreignkey")
    op.drop_index("ix_desktop_runs_parent_run_id", table_name="desktop_runs")
    op.drop_column("desktop_runs", "parent_run_id")
