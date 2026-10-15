"""本文件提供工作区材料排他归属与上传幂等身份的迁移。

输入为既有任务材料表；输出为兼容旧任务归属、可独立拥有工作区材料的 schema。
工作流只添加 nullable 身份、约束和索引，不移动文件或回填历史；存在工作区材料时拒绝降级。
示例：alembic upgrade head。
"""

from alembic import op
import sqlalchemy as sa


revision = "c92f3a4b5d6e"
down_revision = "b81e2f3a4c5d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("desktop_materials", sa.Column("workspace_id", sa.String(32), nullable=True))
    op.add_column("desktop_materials", sa.Column("intake_upload_key", sa.String(65), nullable=True))
    op.alter_column("desktop_materials", "task_id", nullable=True)
    op.create_foreign_key("fk_material_workspace", "desktop_materials", "desktop_workspaces", ["workspace_id"], ["workspace_id"], ondelete="CASCADE")
    op.create_index("ix_desktop_materials_workspace_id", "desktop_materials", ["workspace_id"])
    op.create_unique_constraint("uq_workspace_material_path", "desktop_materials", ["workspace_id", "relative_path"])
    op.create_unique_constraint("uq_material_intake_upload", "desktop_materials", ["intake_upload_key"])
    op.create_check_constraint("ck_material_exclusive_owner", "desktop_materials", "(task_id IS NULL) <> (workspace_id IS NULL)")
    op.create_check_constraint("ck_material_intake_owner", "desktop_materials", "intake_upload_key IS NULL OR workspace_id IS NOT NULL")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM desktop_materials WHERE workspace_id IS NOT NULL)")).scalar():
        raise RuntimeError("存在工作区材料，不能降级并丢失归属；请保留兼容 schema")
    op.drop_constraint("ck_material_intake_owner", "desktop_materials", type_="check")
    op.drop_constraint("ck_material_exclusive_owner", "desktop_materials", type_="check")
    op.drop_constraint("uq_material_intake_upload", "desktop_materials", type_="unique")
    op.drop_constraint("uq_workspace_material_path", "desktop_materials", type_="unique")
    op.drop_index("ix_desktop_materials_workspace_id", table_name="desktop_materials")
    op.drop_constraint("fk_material_workspace", "desktop_materials", type_="foreignkey")
    op.alter_column("desktop_materials", "task_id", nullable=False)
    op.drop_column("desktop_materials", "intake_upload_key")
    op.drop_column("desktop_materials", "workspace_id")
