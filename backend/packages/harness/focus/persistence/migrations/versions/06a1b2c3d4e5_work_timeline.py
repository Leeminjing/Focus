"""Persist the Loop work timeline independently of journal retention."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "06a1b2c3d4e5"
down_revision = "f57e8f901234"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "work_timeline_entries",
        sa.Column("entry_id", sa.String(32), primary_key=True),
        sa.Column("loop_id", sa.String(32), sa.ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_event_id", sa.String(64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(120), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("summary", sa.String(2000), nullable=False),
        sa.Column("round_id", sa.String(32)),
        sa.Column("round_number", sa.Integer()),
        sa.Column("context_ids", postgresql.JSONB(), nullable=False),
        sa.Column("run_id", sa.String(32)),
        sa.Column("fact_id", sa.String(32)),
        sa.Column("fact_revision", sa.Integer()),
        sa.Column("intent_id", sa.String(32)),
        sa.Column("decision_id", sa.String(32)),
        sa.Column("correlation_id", sa.String(120)),
        sa.Column("causation_id", sa.String(64)),
        sa.Column("references", postgresql.JSONB(), nullable=False),
        sa.Column("visibility", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("loop_id", "source_event_id", name="uq_work_timeline_source"),
        sa.UniqueConstraint("loop_id", "sequence", name="uq_work_timeline_sequence"),
    )
    op.create_index("ix_work_timeline_page", "work_timeline_entries", ["loop_id", "sequence"])
    op.create_index("ix_work_timeline_round", "work_timeline_entries", ["loop_id", "round_id", "sequence"])


def downgrade():
    op.drop_table("work_timeline_entries")
