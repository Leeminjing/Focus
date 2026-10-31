"""Persist independently recoverable Observation evidence tasks."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "17b2c3d4e5f6"
down_revision = "06a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("loop_evidence_jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("loop_id", sa.String(32), sa.ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False),
        sa.Column("round_id", sa.String(32), sa.ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False),
        sa.Column("observation_id", sa.String(32), sa.ForeignKey("loop_observations.observation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("observation_generation", sa.Integer(), nullable=False),
        sa.Column("required_source_revisions", JSONB(), nullable=False),
        sa.Column("evidence_kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("result_hash", sa.String(64)),
        sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("budget_reservation", JSONB(), nullable=False, server_default="{}"),
        sa.Column("worker_request_id", sa.String(32), sa.ForeignKey("loop_worker_requests.worker_request_id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("result", JSONB(), nullable=False, server_default="{}"),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('pending','running','ready','failed','cancelled','superseded')", name="ck_evidence_job_status"))
    op.create_index("ix_evidence_job_observation_status", "loop_evidence_jobs", ["observation_id", "status"])
    op.execute("""CREATE TRIGGER loop_evidence_jobs_work_update AFTER UPDATE OF status ON loop_evidence_jobs
        FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status AND NEW.status = 'ready')
        EXECUTE FUNCTION focus_loop_work_notify('rounds,workers')""")


def downgrade():
    op.drop_table("loop_evidence_jobs")
