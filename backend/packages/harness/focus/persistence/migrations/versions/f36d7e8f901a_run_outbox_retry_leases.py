"""为 Run outbox 增加领取租期、耐久退避和持续失败状态。"""

from alembic import op
import sqlalchemy as sa


revision = "f36d7e8f901a"
down_revision = "e25c6d7e8f90"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("run_outbox_events", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("run_outbox_events", sa.Column("next_attempt_at", sa.DateTime(timezone=True)))
    op.drop_constraint("ck_run_outbox_status", "run_outbox_events", type_="check")
    op.create_check_constraint("ck_run_outbox_status", "run_outbox_events",
        "status IN ('pending', 'claimed', 'delivered', 'error', 'failed')")
    op.create_index("ix_run_outbox_ready", "run_outbox_events", ["created_at", "event_id"],
        postgresql_where=sa.text("status IN ('pending', 'error')"))
    op.create_index("ix_run_outbox_claim_expiry", "run_outbox_events", ["lease_expires_at"],
        postgresql_where=sa.text("status = 'claimed'"))


def downgrade():
    op.drop_index("ix_run_outbox_claim_expiry", table_name="run_outbox_events")
    op.drop_index("ix_run_outbox_ready", table_name="run_outbox_events")
    op.execute("UPDATE run_outbox_events SET status = 'error' WHERE status = 'failed'")
    op.drop_constraint("ck_run_outbox_status", "run_outbox_events", type_="check")
    op.create_check_constraint("ck_run_outbox_status", "run_outbox_events",
        "status IN ('pending', 'claimed', 'delivered', 'error')")
    op.drop_column("run_outbox_events", "next_attempt_at")
    op.drop_column("run_outbox_events", "lease_expires_at")
