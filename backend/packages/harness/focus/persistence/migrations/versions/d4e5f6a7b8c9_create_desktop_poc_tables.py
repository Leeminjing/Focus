"""create desktop poc tables

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-08-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "desktop_workspaces",
        sa.Column("workspace_id", sa.String(32), primary_key=True),
        sa.Column("path", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "desktop_threads",
        sa.Column("task_id", sa.String(32), primary_key=True),
        sa.Column("workspace_id", sa.String(32), sa.ForeignKey("desktop_workspaces.workspace_id", ondelete="CASCADE"), nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("ui_state", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "thread_id", name="uq_desktop_task_identity"),
    )
    op.create_table(
        "patrol_drafts",
        sa.Column("draft_id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="editing"),
        sa.Column("system_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("history_messages", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("final_human_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("equipment", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("source_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("token_estimate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_patrol_drafts_task_id", "patrol_drafts", ["task_id"])
    op.create_table(
        "patrol_agents",
        sa.Column("agent_id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False, unique=True),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("frozen_messages", postgresql.JSONB(), nullable=False),
        sa.Column("equipment", postgresql.JSONB(), nullable=False),
        sa.Column("source_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_patrol_agents_task_id", "patrol_agents", ["task_id"])
    op.create_table(
        "desktop_runs",
        sa.Column("run_id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("deployment_id", sa.String(64), nullable=True, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("input_messages", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("model_name", sa.String(120), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_desktop_runs_task_id", "desktop_runs", ["task_id"])
    op.create_index("ix_desktop_runs_agent_id", "desktop_runs", ["agent_id"])
    op.create_table(
        "desktop_materials",
        sa.Column("material_id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("reading_mode", sa.String(16), nullable=False, server_default="full"),
        sa.Column("instruction_mode", sa.String(16), nullable=False, server_default="reference"),
        sa.Column("retention", sa.String(20), nullable=False, server_default="removable"),
        sa.Column("digest", sa.String(64), nullable=True),
        sa.Column("git_ref", sa.Text(), nullable=True),
        sa.Column("needs_confirmation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "relative_path", name="uq_desktop_material_path"),
    )
    op.create_index("ix_desktop_materials_task_id", "desktop_materials", ["task_id"])
    op.create_table(
        "material_versions",
        sa.Column("version_id", sa.String(32), primary_key=True),
        sa.Column("material_id", sa.String(32), sa.ForeignKey("desktop_materials.material_id", ondelete="CASCADE"), nullable=False),
        sa.Column("commit_id", sa.String(64), nullable=False),
        sa.Column("object_id", sa.String(64), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_material_versions_material_id", "material_versions", ["material_id"])


def downgrade() -> None:
    op.drop_table("material_versions")
    op.drop_table("desktop_materials")
    op.drop_table("desktop_runs")
    op.drop_table("patrol_agents")
    op.drop_table("patrol_drafts")
    op.drop_table("desktop_threads")
    op.drop_table("desktop_workspaces")

