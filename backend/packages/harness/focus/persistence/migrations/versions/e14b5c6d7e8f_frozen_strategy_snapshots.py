"""持久保存按内容 hash 寻址的策略正文，目录随 Observation 冻结。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "e14b5c6d7e8f"
down_revision = "d03a4b5c6e7f"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("frozen_strategy_snapshots"):
        return
    op.create_table("frozen_strategy_snapshots",
        sa.Column("content_hash", sa.String(64), primary_key=True),
        sa.Column("payload", JSONB(), nullable=False))


def downgrade():
    # Preserve immutable bodies referenced by already frozen Observations.
    pass
