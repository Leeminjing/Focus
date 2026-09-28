"""本文件对外提供三档文件沙箱模式的数据迁移和回退。

输入为旧版 Desktop 与 Loop 持久化的 workspace/full 字符串以及 swarm_agents 窄列。
输出为 workspace-write/danger-full-access 值和可容纳新模式名称的列。
具体工作流为扩列后迁移直接列、各运行装备和会话 UI 快照；回退时先还原名称再缩列。
示例：alembic upgrade head 将现有 workspace 模式迁移为 workspace-write。
"""

from alembic import op
import sqlalchemy as sa


revision: str = "7b8c9d0e1f2a"
down_revision: str = "6a7b8c9d0e1f"
branch_labels = None
depends_on = None


_EQUIPMENT_TABLES = ("desktop_runs", "patrol_drafts", "patrol_agents", "agent_loops")


def upgrade() -> None:
    op.alter_column(
        "swarm_agents", "access_mode", existing_type=sa.String(16),
        type_=sa.String(32), existing_nullable=False,
        server_default="workspace-write",
    )
    op.execute("UPDATE swarm_agents SET access_mode = CASE access_mode WHEN 'workspace' THEN 'workspace-write' WHEN 'full' THEN 'danger-full-access' ELSE access_mode END")
    for table in _EQUIPMENT_TABLES:
        op.execute(
            f"UPDATE {table} SET equipment = jsonb_set(equipment, '{{access_mode}}', "
            "to_jsonb((CASE equipment->>'access_mode' WHEN 'workspace' THEN 'workspace-write' "
            "WHEN 'full' THEN 'danger-full-access' END)::text), true) "
            "WHERE equipment->>'access_mode' IN ('workspace', 'full')"
        )
    op.execute(
        "UPDATE desktop_threads SET ui_state = jsonb_set(ui_state, '{access_mode}', "
        "to_jsonb((CASE ui_state->>'access_mode' WHEN 'workspace' THEN 'workspace-write' "
        "WHEN 'full' THEN 'danger-full-access' END)::text), true) "
        "WHERE ui_state->>'access_mode' IN ('workspace', 'full')"
    )
    op.execute(
        "UPDATE desktop_threads SET ui_state = jsonb_set(ui_state, '{_main_run_equipment,access_mode}', "
        "to_jsonb((CASE ui_state#>>'{_main_run_equipment,access_mode}' "
        "WHEN 'workspace' THEN 'workspace-write' WHEN 'full' THEN 'danger-full-access' END)::text), true) "
        "WHERE ui_state#>>'{_main_run_equipment,access_mode}' IN ('workspace', 'full')"
    )


def downgrade() -> None:
    op.execute("UPDATE swarm_agents SET access_mode = CASE access_mode WHEN 'workspace-write' THEN 'workspace' WHEN 'danger-full-access' THEN 'full' WHEN 'read-only' THEN 'workspace' ELSE access_mode END")
    for table in _EQUIPMENT_TABLES:
        op.execute(
            f"UPDATE {table} SET equipment = jsonb_set(equipment, '{{access_mode}}', "
            "to_jsonb((CASE equipment->>'access_mode' WHEN 'workspace-write' THEN 'workspace' "
            "WHEN 'danger-full-access' THEN 'full' WHEN 'read-only' THEN 'workspace' END)::text), true) "
            "WHERE equipment->>'access_mode' IN ('workspace-write', 'danger-full-access', 'read-only')"
        )
    for path in ("{access_mode}", "{_main_run_equipment,access_mode}"):
        op.execute(
            f"UPDATE desktop_threads SET ui_state = jsonb_set(ui_state, '{path}', "
            f"to_jsonb((CASE ui_state#>>'{path}' WHEN 'workspace-write' THEN 'workspace' "
            "WHEN 'danger-full-access' THEN 'full' WHEN 'read-only' THEN 'workspace' END)::text), true) "
            f"WHERE ui_state#>>'{path}' IN ('workspace-write', 'danger-full-access', 'read-only')"
        )
    op.alter_column(
        "swarm_agents", "access_mode", existing_type=sa.String(32),
        type_=sa.String(16), existing_nullable=False,
        server_default="workspace",
    )
