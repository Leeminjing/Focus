"""Persist workspace adoption patch identity and before/after fingerprints before filesystem effects."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f36d7e8f9012"
down_revision = "f36d7e8f901a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workspace_adoptions", sa.Column("application_intent", postgresql.JSONB(),
        nullable=False, server_default=sa.text("'{}'::jsonb")))


def downgrade():
    op.drop_column("workspace_adoptions", "application_intent")
