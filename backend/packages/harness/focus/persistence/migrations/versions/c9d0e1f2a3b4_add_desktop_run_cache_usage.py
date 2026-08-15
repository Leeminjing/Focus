"""add desktop run cache usage

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "desktop_runs",
        sa.Column("prompt_input_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "desktop_runs",
        sa.Column("prompt_cache_hit_tokens", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("desktop_runs", "prompt_cache_hit_tokens")
    op.drop_column("desktop_runs", "prompt_input_tokens")
