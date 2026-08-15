"""create desktop context tables

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "desktop_context_definitions",
        sa.Column(
            "context_id",
            sa.String(32),
            sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("authored_messages", postgresql.JSONB(), nullable=False),
        sa.Column("execution_messages", postgresql.JSONB(), nullable=False),
        sa.Column("repair_manifest", postgresql.JSONB(), nullable=False),
        sa.Column("issues", postgresql.JSONB(), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("projection_hash", sa.String(64), nullable=False),
        sa.Column("projection_status", sa.String(32), nullable=False),
        sa.Column("initial_message_ids", postgresql.JSONB(), nullable=False),
        sa.Column("initial_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decided_definition_hash", sa.String(64), nullable=True),
        sa.Column("decided_projection_hash", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_desktop_context_definitions_status",
        "desktop_context_definitions",
        ["projection_status"],
    )
    op.create_table(
        "desktop_context_sources",
        sa.Column("source_id", sa.String(32), primary_key=True),
        sa.Column(
            "context_id",
            sa.String(32),
            sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_context_id",
            sa.String(32),
            sa.ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_checkpoint_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("context_id", "position", name="uq_desktop_context_source_position"),
    )
    op.create_index("ix_desktop_context_sources_context_id", "desktop_context_sources", ["context_id"])
    op.create_index(
        "ix_desktop_context_sources_parent_context_id",
        "desktop_context_sources",
        ["parent_context_id"],
    )


def downgrade() -> None:
    op.drop_table("desktop_context_sources")
    op.drop_table("desktop_context_definitions")
