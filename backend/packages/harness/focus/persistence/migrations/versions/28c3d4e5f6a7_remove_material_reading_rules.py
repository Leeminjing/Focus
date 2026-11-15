"""Remove material reading and instruction policies; retain attachments and versions.

Downgrading restores the former defaults, not the discarded policy selections.
"""

from alembic import op
import sqlalchemy as sa

revision = "28c3d4e5f6a7"
down_revision = "17b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("desktop_materials", "reading_mode")
    op.drop_column("desktop_materials", "instruction_mode")


def downgrade() -> None:
    op.add_column("desktop_materials", sa.Column(
        "reading_mode", sa.String(16), nullable=False, server_default="full",
    ))
    op.add_column("desktop_materials", sa.Column(
        "instruction_mode", sa.String(16), nullable=False, server_default="reference",
    ))
