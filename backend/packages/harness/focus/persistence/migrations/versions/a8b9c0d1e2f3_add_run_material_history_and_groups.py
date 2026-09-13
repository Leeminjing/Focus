"""add run material history and custom groups

本文件对外提供运行材料历史与自定义材料分组的 Alembic 升降级函数。

输入为当前数据库迁移状态；输出为三个新增关系表、约束和查询索引。具体工作流为先创建
自定义组，再创建材料唯一 membership 和不依赖当前材料外键的运行快照；降级按依赖逆序移除，
不会修改 desktop_materials、desktop_runs 或旧图片 equipment。

示例：alembic upgrade a8b9c0d1e2f3。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "f7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "desktop_material_groups",
        sa.Column("group_id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "name", name="uq_desktop_material_group_name"),
        sa.CheckConstraint("position >= 0", name="ck_desktop_material_group_position"),
    )
    op.create_index("ix_desktop_material_groups_task_id", "desktop_material_groups", ["task_id"])
    op.create_table(
        "desktop_material_group_memberships",
        sa.Column("material_id", sa.String(32), sa.ForeignKey("desktop_materials.material_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("group_id", sa.String(32), sa.ForeignKey("desktop_material_groups.group_id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("position >= 0", name="ck_material_group_membership_position"),
    )
    op.create_index("ix_desktop_material_group_memberships_group_id", "desktop_material_group_memberships", ["group_id"])
    op.create_table(
        "desktop_run_material_bindings",
        sa.Column("binding_id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("desktop_runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", sa.String(64), nullable=False),
        sa.Column("material_id_snapshot", sa.String(32), nullable=False),
        sa.Column("relative_path_snapshot", sa.Text(), nullable=False),
        sa.Column("digest_snapshot", sa.String(64), nullable=False),
        sa.Column("material_kind_snapshot", sa.String(24), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("must_view_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "material_id_snapshot", name="uq_run_material_binding_material"),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_run_material_binding_ordinal"),
        sa.CheckConstraint("ordinal >= 0", name="ck_run_material_binding_ordinal"),
    )
    op.create_index("ix_desktop_run_material_bindings_run_id", "desktop_run_material_bindings", ["run_id"])
    op.create_index("ix_desktop_run_material_bindings_task_id", "desktop_run_material_bindings", ["task_id"])
    op.create_index("ix_desktop_run_material_bindings_message_id", "desktop_run_material_bindings", ["message_id"])
    op.create_index(
        "ix_run_material_history_task_material_created",
        "desktop_run_material_bindings",
        ["task_id", "material_id_snapshot", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("desktop_run_material_bindings")
    op.drop_table("desktop_material_group_memberships")
    op.drop_table("desktop_material_groups")
