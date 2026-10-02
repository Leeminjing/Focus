"""本文件对外提供 Patrol authoring 的 additive upgrade 和受保护 downgrade。

输入为执行审计 schema，输出为可并发草稿及数据库不可变部署定义；旧正文与 checkpoint 不修改。
工作流为增加可空文档、版本和冻结来源列，建立定义表及拒绝 UPDATE/DELETE 的触发器。
示例：alembic upgrade head；已有定义时 downgrade 拒绝删除审计事实。
定义 run_id 保留审计关联但不使用可阻断删除的外键；创建由部署事务保障，删除 Run 不删除或改写冻结定义。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "c36f7a8b9c0d"
down_revision = "c25e6f7a8b9c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("patrol_drafts", sa.Column("authoring_document", JSONB(), nullable=True))
    op.add_column("patrol_drafts", sa.Column("draft_revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("patrol_drafts", sa.Column("frozen_sources", JSONB(), nullable=False, server_default="{}"))
    op.create_table("patrol_deployment_definitions",
        sa.Column("definition_id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), unique=True, nullable=False),
        sa.Column("document_hash", sa.String(64), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        sa.Column("compiled_plan", JSONB(), nullable=False),
        sa.Column("sources", JSONB(), nullable=False),
        sa.Column("execution_mode", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.execute("""CREATE FUNCTION protect_patrol_definition() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Patrol deployment definitions are immutable'; END; $$""")
    op.execute("CREATE TRIGGER patrol_definition_immutable BEFORE UPDATE OR DELETE ON patrol_deployment_definitions FOR EACH ROW EXECUTE FUNCTION protect_patrol_definition()")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM patrol_deployment_definitions)")):
        raise RuntimeError("已有部署定义，不能破坏执行审计；请暂停新投放并保留 schema")
    op.drop_table("patrol_deployment_definitions")
    op.execute("DROP FUNCTION protect_patrol_definition()")
    for name in ("frozen_sources", "draft_revision", "authoring_document"):
        op.drop_column("patrol_drafts", name)
