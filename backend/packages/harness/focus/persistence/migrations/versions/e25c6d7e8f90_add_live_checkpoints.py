"""Persist sequence-bound Live projection checkpoints."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e25c6d7e8f90"
down_revision = "e14b5c6d7e8f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("loop_live_checkpoints",
        sa.Column("loop_id", sa.String(32), sa.ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("projection", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade():
    op.drop_table("loop_live_checkpoints")
