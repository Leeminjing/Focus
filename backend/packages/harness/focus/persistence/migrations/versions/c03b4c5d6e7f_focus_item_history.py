"""本文件对外提供 V2 history 的扩展式数据库迁移。

输入为 Alembic 连接与 bf2a3b4c5d6e schema；输出为可空历史 envelope 列，不改旧内容和哈希。
具体工作流为增加 JSONB 列，旧行 NULL 明确表示 V1；回退只用于停止 V2 writer 后的离线管理。
示例：alembic upgrade head。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c03b4c5d6e7f"
down_revision = "bf2a3b4c5d6e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("desktop_context_revisions", sa.Column("history_payload", postgresql.JSONB(), nullable=True))


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM desktop_context_revisions WHERE history_payload IS NOT NULL AND history_payload <> 'null'::jsonb)")):
        raise RuntimeError("存在 V2 历史，不能删除权威载荷；请保留兼容 reader")
    op.drop_column("desktop_context_revisions", "history_payload")
