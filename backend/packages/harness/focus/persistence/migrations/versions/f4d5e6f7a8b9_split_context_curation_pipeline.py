"""split context curation observation, revision and attempt

Revision ID: f4d5e6f7a8b9
Revises: f3c4d5e6f7a8
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "f3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_patrol_context_binding_status", "patrol_context_bindings", type_="check"
    )
    op.alter_column(
        "patrol_context_bindings", "tracking_status", new_column_name="control_state"
    )
    op.add_column(
        "patrol_context_bindings",
        sa.Column("health_state", sa.String(length=16), server_default="idle", nullable=False),
    )
    op.add_column("patrol_context_bindings", sa.Column("observed_checkpoint_id", sa.Text()))
    op.add_column("patrol_context_bindings", sa.Column("prepared_checkpoint_id", sa.Text()))
    op.execute(
        "UPDATE patrol_context_bindings SET observed_checkpoint_id = desired_checkpoint_id, "
        "prepared_checkpoint_id = CASE WHEN desired_checkpoint_id = published_checkpoint_id "
        "THEN published_checkpoint_id ELSE NULL END, "
        "health_state = CASE WHEN control_state = 'blocked' THEN 'blocked' ELSE 'idle' END, "
        "control_state = CASE WHEN control_state = 'blocked' THEN 'following' ELSE control_state END"
    )
    op.create_check_constraint(
        "ck_patrol_context_binding_control_state",
        "patrol_context_bindings",
        "control_state IN ('following', 'paused', 'sealed', 'stopped')",
    )
    op.create_check_constraint(
        "ck_patrol_context_binding_health_state",
        "patrol_context_bindings",
        "health_state IN ('idle', 'preparing', 'running', 'degraded', 'blocked')",
    )

    op.drop_constraint(
        "ck_patrol_context_revision_status", "patrol_context_revisions", type_="check"
    )
    op.alter_column(
        "patrol_context_revisions", "input_payload", new_column_name="source_payload"
    )
    op.add_column(
        "patrol_context_revisions", sa.Column("source_projection_hash", sa.String(length=64))
    )
    op.add_column(
        "patrol_context_revisions", sa.Column("prepared_at", sa.DateTime(timezone=True))
    )

    op.create_table(
        "patrol_context_attempts",
        sa.Column("attempt_id", sa.String(length=32), nullable=False),
        sa.Column("revision_id", sa.String(length=32), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("output_method", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "raw_response", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column(
            "parsed_response", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
        sa.Column("error_kind", sa.String(length=32)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'success', 'error', 'interrupted', 'superseded')",
            name="ck_patrol_context_attempt_status",
        ),
        sa.CheckConstraint(
            "output_method IN ('json_schema', 'json_mode', 'prompt_json')",
            name="ck_patrol_context_attempt_output_method",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["patrol_context_revisions.revision_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["desktop_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("revision_id", "attempt_number", name="uq_patrol_context_attempt_number"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index(
        "ix_patrol_context_attempts_revision_id", "patrol_context_attempts", ["revision_id"]
    )
    op.execute(
        "INSERT INTO patrol_context_attempts "
        "(attempt_id, revision_id, attempt_number, run_id, model_name, output_method, status, "
        "error_kind, error, created_at, started_at, completed_at) "
        "SELECT substr(md5(revision_id || ':attempt:1'), 1, 32), revision_id, 1, run_id, "
        "COALESCE((SELECT model_name FROM desktop_runs WHERE desktop_runs.run_id = "
        "patrol_context_revisions.run_id), 'unknown'), 'prompt_json', "
        "CASE status WHEN 'running' THEN 'running' WHEN 'interrupted' THEN 'interrupted' "
        "WHEN 'error' THEN 'error' WHEN 'superseded' THEN 'superseded' ELSE 'success' END, "
        "CASE WHEN status = 'error' THEN 'legacy' ELSE NULL END, error, created_at, started_at, completed_at "
        "FROM patrol_context_revisions WHERE run_id IS NOT NULL"
    )
    op.execute(
        "UPDATE patrol_context_revisions SET prepared_at = started_at, "
        "status = CASE status WHEN 'pending' THEN 'observed' WHEN 'running' THEN 'ready' ELSE status END"
    )
    op.drop_constraint(
        "patrol_context_revisions_attempt_checkpoint_ns_key",
        "patrol_context_revisions",
        type_="unique",
    )
    op.drop_constraint(
        "patrol_context_revisions_run_id_key", "patrol_context_revisions", type_="unique"
    )
    op.drop_column("patrol_context_revisions", "attempt_checkpoint_ns")
    op.drop_column("patrol_context_revisions", "run_id")
    op.drop_column("patrol_context_revisions", "started_at")
    op.create_check_constraint(
        "ck_patrol_context_revision_status",
        "patrol_context_revisions",
        "status IN ('observed', 'preparing', 'ready', 'publishing', 'superseded', "
        "'approval_required', 'published', 'error', 'interrupted')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_patrol_context_revision_status", "patrol_context_revisions", type_="check"
    )
    op.add_column("patrol_context_revisions", sa.Column("started_at", sa.DateTime(timezone=True)))
    op.add_column("patrol_context_revisions", sa.Column("attempt_checkpoint_ns", sa.Text()))
    op.add_column("patrol_context_revisions", sa.Column("run_id", sa.String(length=32)))
    op.execute(
        "UPDATE patrol_context_revisions r SET run_id = a.run_id, started_at = a.started_at "
        "FROM patrol_context_attempts a WHERE a.revision_id = r.revision_id AND a.attempt_number = "
        "(SELECT max(a2.attempt_number) FROM patrol_context_attempts a2 WHERE a2.revision_id = r.revision_id)"
    )
    op.create_unique_constraint(
        "patrol_context_revisions_run_id_key", "patrol_context_revisions", ["run_id"]
    )
    op.create_unique_constraint(
        "patrol_context_revisions_attempt_checkpoint_ns_key",
        "patrol_context_revisions",
        ["attempt_checkpoint_ns"],
    )
    op.create_foreign_key(
        "patrol_context_revisions_run_id_fkey",
        "patrol_context_revisions",
        "desktop_runs",
        ["run_id"],
        ["run_id"],
        ondelete="SET NULL",
    )
    op.execute(
        "UPDATE patrol_context_revisions SET status = CASE status "
        "WHEN 'observed' THEN 'pending' WHEN 'ready' THEN 'pending' WHEN 'preparing' THEN 'pending' "
        "ELSE status END"
    )
    op.create_check_constraint(
        "ck_patrol_context_revision_status",
        "patrol_context_revisions",
        "status IN ('pending', 'running', 'publishing', 'superseded', "
        "'approval_required', 'published', 'error', 'interrupted')",
    )
    op.drop_index("ix_patrol_context_attempts_revision_id", table_name="patrol_context_attempts")
    op.drop_table("patrol_context_attempts")
    op.drop_column("patrol_context_revisions", "prepared_at")
    op.drop_column("patrol_context_revisions", "source_projection_hash")
    op.alter_column(
        "patrol_context_revisions", "source_payload", new_column_name="input_payload"
    )

    op.drop_constraint(
        "ck_patrol_context_binding_health_state", "patrol_context_bindings", type_="check"
    )
    op.drop_constraint(
        "ck_patrol_context_binding_control_state", "patrol_context_bindings", type_="check"
    )
    op.execute(
        "UPDATE patrol_context_bindings SET control_state = 'blocked' WHERE health_state = 'blocked'"
    )
    op.drop_column("patrol_context_bindings", "prepared_checkpoint_id")
    op.drop_column("patrol_context_bindings", "observed_checkpoint_id")
    op.drop_column("patrol_context_bindings", "health_state")
    op.alter_column(
        "patrol_context_bindings", "control_state", new_column_name="tracking_status"
    )
    op.create_check_constraint(
        "ck_patrol_context_binding_status",
        "patrol_context_bindings",
        "tracking_status IN ('following', 'paused', 'blocked', 'sealed', 'stopped')",
    )
