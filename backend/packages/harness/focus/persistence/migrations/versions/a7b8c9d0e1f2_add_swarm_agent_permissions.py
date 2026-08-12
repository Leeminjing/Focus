"""add swarm agent permissions column

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # wake 复用 spawn 时的权限（不放大），持久化到 swarm_agents.permissions
    op.add_column(
        "swarm_agents",
        sa.Column(
            "permissions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[\"read\"]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("swarm_agents", "permissions")
