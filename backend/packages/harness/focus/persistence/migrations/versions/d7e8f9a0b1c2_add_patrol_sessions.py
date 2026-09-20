r"""本迁移对外提供可恢复 Patrol Session 与不可变 phase history。

输入为已有 Loop round 和 canonical journal schema；输出为 `loop_patrol_sessions` 当前状态表与
`loop_patrol_phase_transitions` 历史表。具体工作流为按外键顺序创建，downgrade 反序移除。
示例：`alembic upgrade head`。
"""

from alembic import op

import backend.app.desktop.persistence_registry
from focus.persistence.base import Base


revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["loop_patrol_sessions"].create(bind=bind, checkfirst=False)
    Base.metadata.tables["loop_patrol_phase_transitions"].create(bind=bind, checkfirst=False)
    Base.metadata.tables["loop_curator_assignments"].create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["loop_curator_assignments"].drop(bind=bind, checkfirst=False)
    Base.metadata.tables["loop_patrol_phase_transitions"].drop(bind=bind, checkfirst=False)
    Base.metadata.tables["loop_patrol_sessions"].drop(bind=bind, checkfirst=False)
