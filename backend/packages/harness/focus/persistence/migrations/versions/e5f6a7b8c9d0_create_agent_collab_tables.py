"""create agent collab tables

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-06
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_messages",
        sa.Column("message_id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_agent", sa.String(64), nullable=False),
        sa.Column("to_agent", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False, server_default="message"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_messages_task_id", "agent_messages", ["task_id"])
    op.create_index("ix_agent_messages_from_agent", "agent_messages", ["from_agent"])
    op.create_index("ix_agent_messages_to_agent", "agent_messages", ["to_agent"])
    op.create_table(
        "agent_tasks",
        sa.Column("board_task_id", sa.String(32), primary_key=True),
        sa.Column("thread_task_id", sa.String(32), sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requirements", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_tasks_thread_task_id", "agent_tasks", ["thread_task_id"])


def downgrade() -> None:
    op.drop_table("agent_tasks")
    op.drop_table("agent_messages")
