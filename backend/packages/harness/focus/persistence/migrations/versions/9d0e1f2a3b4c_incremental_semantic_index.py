"""本文件对外提供单 segment 投影记录的 additive 迁移。

输入为 round memory 版数据库；输出为按 Context／依赖键唯一的不可变投影记录表。
工作流为只增加 records；Revision index 的继承 proof 保存在既有 JSON payload，不伪造旧验证记录。
示例：alembic upgrade head；downgrade 清理新合同 index 后删除 records，不修改旧 index 或 Context／Progress。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "9d0e1f2a3b4c"
down_revision = "8c9d0e1f2a3b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "loop_segment_projection_records",
        sa.Column("record_id", sa.String(64), primary_key=True),
        sa.Column(
            "context_id",
            sa.String(32),
            sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "context_id", "cache_key", name="uq_loop_segment_projection_key"
        ),
    )
    op.create_index(
        "ix_loop_segment_projection_records_context_id",
        "loop_segment_projection_records",
        ["context_id"],
    )


def downgrade():
    op.execute(
        sa.text(
            "DELETE FROM loop_semantic_index_artifacts WHERE index_schema_version = 'revision-semantic-index-v4' AND payload->'inheritance' IS NOT NULL AND payload->'inheritance' <> 'null'::jsonb"
        )
    )
    op.drop_table("loop_segment_projection_records")
