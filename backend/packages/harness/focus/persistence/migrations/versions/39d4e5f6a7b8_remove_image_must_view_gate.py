"""Remove the image declaration flag while retaining material bindings and notes."""

from alembic import op
import sqlalchemy as sa

revision = "39d4e5f6a7b8"
down_revision = "28c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("desktop_run_material_bindings", "must_view_requested")


def downgrade() -> None:
    op.add_column("desktop_run_material_bindings", sa.Column(
        "must_view_requested", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
