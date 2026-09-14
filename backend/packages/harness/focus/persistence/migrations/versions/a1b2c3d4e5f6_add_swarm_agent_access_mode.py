"""add swarm agent access mode column

Revision ID: a1b2c3d4e5f6
Revises: a8b9c0d1e2f3
Create Date: 2026-09-14

持久派生 Agent 的本地访问模式：spawn 时从父级安全上下文继承，wake 沿用（不放大）。
服务器默认取最严的工作区保护，使本次改动之前创建的 Agent 在 wake 后不会获得更宽的本机资源准入。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "swarm_agents",
        sa.Column(
            "access_mode",
            sa.String(16),
            nullable=False,
            server_default="workspace",
        ),
    )


def downgrade() -> None:
    op.drop_column("swarm_agents", "access_mode")
