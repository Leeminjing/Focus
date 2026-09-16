r"""本文件对外提供 Agent Loop authority、Kernel、observation、completion 与 event 表的可逆迁移。

输入为 workspace coordination revision `a9b0c1d2e3f4`；输出为 Loop 聚合的全部分表、外键、检查约束
和唯一 writer 索引。具体工作流为从统一 ORM metadata 按依赖顺序创建本 revision 专属表，downgrade
按逆序删除，避免手写 DDL 与领域实体漂移。示例：`alembic upgrade b0c1d2e3f4a5`。
"""

from typing import Sequence, Union

from alembic import op

import backend.app.desktop.persistence_registry
from focus.persistence.base import Base


revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "a9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = (
    "agent_loops",
    "loop_goal_revisions",
    "loop_delegation_grants",
    "loop_context_memberships",
    "loop_rounds",
    "loop_observations",
    "loop_patrol_attempts",
    "loop_decisions",
    "loop_actions",
    "loop_directives",
    "message_provenance",
    "loop_budget_usage",
    "loop_worker_requests",
    "loop_completion_verifications",
    "loop_pending_decisions",
    "loop_event_outbox",
    "loop_coordinator_leases",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=False)
