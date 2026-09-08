"""allow unchanged context curation revisions

Revision ID: f5e6f7a8b9c0
Revises: f4d5e6f7a8b9
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "f4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_patrol_context_revision_status", "patrol_context_revisions", type_="check"
    )
    op.create_check_constraint(
        "ck_patrol_context_revision_status",
        "patrol_context_revisions",
        "status IN ('observed', 'preparing', 'ready', 'publishing', 'superseded', "
        "'approval_required', 'published', 'unchanged', 'error', 'interrupted')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE patrol_context_revisions SET status = 'published' WHERE status = 'unchanged'"
    )
    op.drop_constraint(
        "ck_patrol_context_revision_status", "patrol_context_revisions", type_="check"
    )
    op.create_check_constraint(
        "ck_patrol_context_revision_status",
        "patrol_context_revisions",
        "status IN ('observed', 'preparing', 'ready', 'publishing', 'superseded', "
        "'approval_required', 'published', 'error', 'interrupted')",
    )
