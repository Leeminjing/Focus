r"""本迁移对外提供冻结 Observation 的 projection sequence 与基础实体版本列。

输入为已有 loop_observations；输出为非空 sequence 和 JSON 基础版本集合。具体工作流为 upgrade 追加带安全默认值的
列，既有记录保持可读，downgrade 反序移除。示例：`alembic upgrade head`。
"""

from alembic import op


revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE loop_observations ADD COLUMN IF NOT EXISTS projection_sequence BIGINT DEFAULT 0 NOT NULL")
    op.execute("ALTER TABLE loop_observations ADD COLUMN IF NOT EXISTS base_entity_revisions JSONB DEFAULT '{}'::jsonb NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE loop_observations DROP COLUMN IF EXISTS base_entity_revisions")
    op.execute("ALTER TABLE loop_observations DROP COLUMN IF EXISTS projection_sequence")
