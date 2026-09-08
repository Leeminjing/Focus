"""add context curator patrol

Revision ID: f3c4d5e6f7a8
Revises: f2b3c4d5e6f7
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "f2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "patrol_drafts",
        sa.Column("mode", sa.String(length=24), server_default="standard", nullable=False),
    )
    op.add_column(
        "patrol_drafts",
        sa.Column(
            "curation_policy", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
    )
    op.add_column(
        "patrol_agents",
        sa.Column("mode", sa.String(length=24), server_default="standard", nullable=False),
    )
    op.add_column(
        "patrol_agents",
        sa.Column(
            "curation_policy", postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
    )

    op.create_table(
        "patrol_context_bindings",
        sa.Column("binding_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("root_context_id", sa.String(length=32), nullable=False),
        sa.Column("managed_context_id", sa.String(length=32), nullable=True),
        sa.Column("tracking_status", sa.String(length=16), nullable=False),
        sa.Column("desired_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("published_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "tracking_status IN ('following', 'paused', 'blocked', 'sealed', 'stopped')",
            name="ck_patrol_context_binding_status",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["patrol_agents.agent_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("binding_id"),
        sa.UniqueConstraint("agent_id"),
        sa.UniqueConstraint("managed_context_id"),
    )
    op.create_index("ix_patrol_context_bindings_agent_id", "patrol_context_bindings", ["agent_id"])
    op.create_index("ix_patrol_context_bindings_root_context_id", "patrol_context_bindings", ["root_context_id"])
    op.create_index("ix_patrol_context_bindings_managed_context_id", "patrol_context_bindings", ["managed_context_id"])

    op.create_table(
        "patrol_context_revisions",
        sa.Column("revision_id", sa.String(length=32), nullable=False),
        sa.Column("binding_id", sa.String(length=32), nullable=False),
        sa.Column("source_checkpoint_id", sa.Text(), nullable=False),
        sa.Column("base_binding_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("attempt_checkpoint_ns", sa.Text(), nullable=True),
        sa.Column("input_payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("authored_messages", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("execution_messages", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("disposition_manifest", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("repair_manifest", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("issues", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=True),
        sa.Column("projection_hash", sa.String(length=64), nullable=True),
        sa.Column("projection_status", sa.String(length=32), nullable=True),
        sa.Column("published_context_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'publishing', 'superseded', "
            "'approval_required', 'published', 'error', 'interrupted')",
            name="ck_patrol_context_revision_status",
        ),
        sa.ForeignKeyConstraint(["binding_id"], ["patrol_context_bindings.binding_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["desktop_runs.run_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint("binding_id", "source_checkpoint_id", name="uq_patrol_context_revision_source"),
        sa.UniqueConstraint("run_id"),
        sa.UniqueConstraint("attempt_checkpoint_ns"),
    )
    op.create_index("ix_patrol_context_revisions_binding_id", "patrol_context_revisions", ["binding_id"])


def downgrade() -> None:
    op.drop_index("ix_patrol_context_revisions_binding_id", table_name="patrol_context_revisions")
    op.drop_table("patrol_context_revisions")
    op.drop_index("ix_patrol_context_bindings_managed_context_id", table_name="patrol_context_bindings")
    op.drop_index("ix_patrol_context_bindings_root_context_id", table_name="patrol_context_bindings")
    op.drop_index("ix_patrol_context_bindings_agent_id", table_name="patrol_context_bindings")
    op.drop_table("patrol_context_bindings")
    op.drop_column("patrol_agents", "curation_policy")
    op.drop_column("patrol_agents", "mode")
    op.drop_column("patrol_drafts", "curation_policy")
    op.drop_column("patrol_drafts", "mode")
