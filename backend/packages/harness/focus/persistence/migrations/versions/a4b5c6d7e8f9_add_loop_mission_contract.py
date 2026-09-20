r"""本迁移对外提供版本化 Loop Mission contract 持久化结构。

输入为现有 `loop_goal_revisions` 与 Agent Loop schema；输出为分别保存最终结果、执行边界、完成检查及
旧 Goal 来源的 `loop_mission_revisions` 表。具体工作流为 upgrade 创建纯 additive 表且不改写旧记录，
downgrade 仅移除新表，从而保留全部历史 Goal 审计数据。示例：`alembic upgrade head`。
"""

from collections.abc import Sequence

from alembic import op

import backend.app.desktop.persistence_registry
from focus.persistence.base import Base


revision: str = "a4b5c6d7e8f9"
down_revision: str | Sequence[str] | None = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.tables["loop_mission_revisions"].create(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.tables["loop_mission_revisions"].drop(bind=op.get_bind(), checkfirst=False)
