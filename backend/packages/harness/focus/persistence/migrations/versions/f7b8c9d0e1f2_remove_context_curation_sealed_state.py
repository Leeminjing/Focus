"""remove automatic context curation sealed state

Revision ID: f7b8c9d0e1f2
Revises: f5e6f7a8b9c0
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE patrol_context_bindings "
        "SET control_state = 'following', sealed_at = NULL "
        "WHERE control_state = 'sealed'"
    )
    op.drop_constraint(
        "ck_patrol_context_binding_control_state",
        "patrol_context_bindings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_patrol_context_binding_control_state",
        "patrol_context_bindings",
        "control_state IN ('following', 'paused', 'stopped')",
    )
    op.drop_column("patrol_context_bindings", "sealed_at")


def downgrade() -> None:
    op.add_column(
        "patrol_context_bindings",
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "ck_patrol_context_binding_control_state",
        "patrol_context_bindings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_patrol_context_binding_control_state",
        "patrol_context_bindings",
        "control_state IN ('following', 'paused', 'sealed', 'stopped')",
    )
