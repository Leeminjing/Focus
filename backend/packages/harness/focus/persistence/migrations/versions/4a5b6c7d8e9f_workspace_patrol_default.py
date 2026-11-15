r"""本文件对外提供 Workspace Patrol 唯一新建模式的默认值迁移。

输入为 revision `39d4e5f6a7b8` 的 Loop schema；输出为 interaction_mode 默认 workspace_patrol。
具体工作流为只修改列默认，不改变已有 context_loop 历史身份或数据；旧运行实例
在正常应用启动前通过共享终态生命周期收敛。downgrade 只恢复旧列默认。
示例：`alembic upgrade 4a5b6c7d8e9f`。
"""

from alembic import op


revision = "4a5b6c7d8e9f"
down_revision = "39d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("agent_loops", "interaction_mode", server_default="workspace_patrol")


def downgrade() -> None:
    op.alter_column("agent_loops", "interaction_mode", server_default="context_loop")
