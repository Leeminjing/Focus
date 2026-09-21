r"""本文件对外提供可靠 Context expansion 生命周期的增量数据库迁移。

输入为 revision `2c3d4e5f6a7b` 的 Agent Loop、round、Context revision 与 journal 表；输出为 expansion 当前状态和不可变
transition 历史表及去重索引。具体工作流为只新增表、外键和索引，不改写现有 Loop 数据；downgrade 按依赖逆序删除新增结构。
示例：`alembic upgrade 3d4e5f6a7b8c`。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "3d4e5f6a7b8c"
down_revision = "2c3d4e5f6a7b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loop_context_expansions",
        sa.Column("expansion_id", sa.String(length=64), nullable=False),
        sa.Column("opportunity_id", sa.String(length=64), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("round_id", sa.String(length=32), nullable=False),
        sa.Column("source_context_id", sa.String(length=32), nullable=False),
        sa.Column("source_revision_id", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=24), server_default="detected", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=24), nullable=False),
        sa.Column("independence_key", sa.String(length=240), nullable=False),
        sa.Column("semantic_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("workspace_mode", sa.String(length=24), nullable=False),
        sa.Column("opportunity", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blocker_code", sa.String(length=64), nullable=True),
        sa.Column("safe_summary", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=False),
        sa.Column("causation_id", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["round_id"], ["loop_rounds.round_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_context_id"], ["desktop_threads.task_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_revision_id"], ["desktop_context_revisions.revision_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("expansion_id"),
        sa.UniqueConstraint("loop_id", "opportunity_id", name="uq_loop_context_expansion_opportunity"),
    )
    op.create_index("ix_loop_context_expansions_loop_id", "loop_context_expansions", ["loop_id"], unique=False)
    op.create_index("ix_loop_context_expansions_round_id", "loop_context_expansions", ["round_id"], unique=False)
    op.create_index("ix_loop_context_expansions_source_context_id", "loop_context_expansions", ["source_context_id"], unique=False)
    op.create_index("ix_loop_context_expansions_semantic_fingerprint", "loop_context_expansions", ["semantic_fingerprint"], unique=False)
    op.create_table(
        "loop_context_expansion_transitions",
        sa.Column("transition_id", sa.String(length=32), nullable=False),
        sa.Column("expansion_id", sa.String(length=64), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("round_id", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=24), nullable=False),
        sa.Column("to_state", sa.String(length=24), nullable=False),
        sa.Column("blocker_code", sa.String(length=64), nullable=True),
        sa.Column("safe_summary", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["expansion_id"], ["loop_context_expansions.expansion_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["round_id"], ["loop_rounds.round_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("transition_id"),
        sa.UniqueConstraint("expansion_id", "revision", name="uq_loop_context_expansion_transition_revision"),
    )
    op.create_index("ix_loop_context_expansion_transitions_expansion_id", "loop_context_expansion_transitions", ["expansion_id"], unique=False)
    op.create_index("ix_loop_context_expansion_transitions_loop_id", "loop_context_expansion_transitions", ["loop_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_loop_context_expansion_transitions_loop_id", table_name="loop_context_expansion_transitions")
    op.drop_index("ix_loop_context_expansion_transitions_expansion_id", table_name="loop_context_expansion_transitions")
    op.drop_table("loop_context_expansion_transitions")
    op.drop_index("ix_loop_context_expansions_semantic_fingerprint", table_name="loop_context_expansions")
    op.drop_index("ix_loop_context_expansions_source_context_id", table_name="loop_context_expansions")
    op.drop_index("ix_loop_context_expansions_round_id", table_name="loop_context_expansions")
    op.drop_index("ix_loop_context_expansions_loop_id", table_name="loop_context_expansions")
    op.drop_table("loop_context_expansions")
