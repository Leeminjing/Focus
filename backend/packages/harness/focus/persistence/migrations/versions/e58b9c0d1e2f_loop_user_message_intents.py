"""本文件对外提供 Loop 用户消息类型及原始请求的增量迁移。

输入为 d47a8b9c0d1e 数据库；输出为默认普通意见的 intent_kind 和空 request_payload。
具体工作流为保留所有历史记录，新增类型及不可变请求来源；存在新来源时拒绝删除审计的降级。
示例：alembic upgrade e58b9c0d1e2f，仅在批准的隔离数据库部署。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e58b9c0d1e2f"
down_revision = "d47a8b9c0d1e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("loop_user_intents", sa.Column("intent_kind", sa.String(24), nullable=False,
                                               server_default="patrol_opinion"))
    op.add_column("loop_user_intents", sa.Column("request_payload", postgresql.JSONB(), nullable=False,
                                               server_default=sa.text("'{}'::jsonb")))


def downgrade():
    retained = op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM loop_user_intents WHERE request_payload <> '{}'::jsonb)")).scalar()
    if retained:
        raise RuntimeError("回退会删除用户消息原始请求审计；请保留增量 schema 回退软件")
    op.drop_column("loop_user_intents", "request_payload")
    op.drop_column("loop_user_intents", "intent_kind")
