"""add canonical loop event journal

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loop_journal_sequences",
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("loop_id"),
    )
    op.create_table(
        "loop_replay_retention",
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("minimum_sequence", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("retention_seconds", sa.Integer(), server_default="604800", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("loop_id"),
    )
    op.create_table(
        "loop_projector_cursors",
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("projector_name", sa.String(length=80), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("loop_id", "projector_name"),
    )
    op.create_table(
        "loop_journal_events",
        sa.Column("event_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("kind", sa.String(length=120), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.String(length=120), nullable=False),
        sa.Column("entity_revision", sa.BigInteger(), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("causation_id", sa.String(length=120), nullable=True),
        sa.Column("visibility", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("retained_until", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("loop_id", "sequence", name="uq_loop_journal_sequence"),
        sa.UniqueConstraint("loop_id", "idempotency_key", name="uq_loop_journal_idempotency"),
        sa.UniqueConstraint("loop_id", "kind", "entity_type", "entity_id", "entity_revision", name="uq_loop_journal_entity_transition"),
    )
    op.create_index("ix_loop_journal_replay", "loop_journal_events", ["loop_id", "sequence"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_loop_journal_replay", table_name="loop_journal_events")
    op.drop_table("loop_journal_events")
    op.drop_table("loop_projector_cursors")
    op.drop_table("loop_replay_retention")
    op.drop_table("loop_journal_sequences")
