r"""本迁移对外提供版本化 Loop Fact、不可变 revision history 与事实关系表。

输入为已存在的 Agent Loop、Context 与 Run；输出为 additive `loop_facts`、`loop_fact_revisions` 和
`loop_fact_relationships`。具体工作流为先创建 current fact，再创建 revision 和 self-referencing relationship；
downgrade 仅反序移除新增表，不改变旧 Loop 数据。示例：`alembic upgrade head`。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0a1b2c3d4e5f"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loop_facts",
        sa.Column("fact_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("identity_key", sa.String(length=64), nullable=False),
        sa.Column("fact_type", sa.String(length=32), nullable=False),
        sa.Column("normalized_subject", sa.String(length=500), nullable=False),
        sa.Column("current_revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("state", sa.String(length=24), server_default="observed", nullable=False),
        sa.Column("source_context_id", sa.String(length=32), nullable=True),
        sa.Column("source_run_id", sa.String(length=32), nullable=True),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("presentation", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("observer", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("verifier", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("state IN ('observed','verifying','verified','contradicted','superseded')", name="ck_loop_fact_state"),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_context_id"], ["desktop_threads.task_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_run_id"], ["desktop_runs.run_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("fact_id"),
        sa.UniqueConstraint("loop_id", "identity_key", name="uq_loop_fact_identity"),
    )
    op.create_index("ix_loop_facts_loop_id", "loop_facts", ["loop_id"])
    op.create_index("ix_loop_facts_fact_type", "loop_facts", ["fact_type"])
    op.create_index("ix_loop_facts_source_context_id", "loop_facts", ["source_context_id"])
    op.create_index("ix_loop_facts_source_run_id", "loop_facts", ["source_run_id"])
    op.create_index("ix_loop_facts_correlation_id", "loop_facts", ["correlation_id"])
    op.create_index("ix_loop_fact_current_subject", "loop_facts", ["loop_id", "fact_type", "normalized_subject", "updated_at"])
    op.create_table(
        "loop_fact_revisions",
        sa.Column("fact_revision_id", sa.String(length=32), nullable=False),
        sa.Column("fact_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("cause_event_id", sa.String(length=32), nullable=True),
        sa.Column("correlation_id", sa.String(length=120), nullable=True),
        sa.Column("presentation", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("observer", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("verifier", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("state IN ('observed','verifying','verified','contradicted','superseded')", name="ck_loop_fact_revision_state"),
        sa.ForeignKeyConstraint(["fact_id"], ["loop_facts.fact_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("fact_revision_id"),
        sa.UniqueConstraint("fact_id", "revision", name="uq_loop_fact_revision"),
    )
    op.create_index("ix_loop_fact_revisions_fact_id", "loop_fact_revisions", ["fact_id"])
    op.create_index("ix_loop_fact_revisions_loop_id", "loop_fact_revisions", ["loop_id"])
    op.create_table(
        "loop_fact_relationships",
        sa.Column("relationship_id", sa.String(length=32), nullable=False),
        sa.Column("loop_id", sa.String(length=32), nullable=False),
        sa.Column("source_fact_id", sa.String(length=32), nullable=False),
        sa.Column("target_fact_id", sa.String(length=32), nullable=False),
        sa.Column("relation", sa.String(length=24), nullable=False),
        sa.Column("cause_event_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("relation IN ('supersedes','contradicts')", name="ck_loop_fact_relationship_kind"),
        sa.ForeignKeyConstraint(["loop_id"], ["agent_loops.loop_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_fact_id"], ["loop_facts.fact_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_fact_id"], ["loop_facts.fact_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("relationship_id"),
        sa.UniqueConstraint("source_fact_id", "target_fact_id", "relation", name="uq_loop_fact_relationship"),
    )
    op.create_index("ix_loop_fact_relationships_loop_id", "loop_fact_relationships", ["loop_id"])
    op.create_index("ix_loop_fact_relationships_source_fact_id", "loop_fact_relationships", ["source_fact_id"])
    op.create_index("ix_loop_fact_relationships_target_fact_id", "loop_fact_relationships", ["target_fact_id"])


def downgrade() -> None:
    op.drop_index("ix_loop_fact_relationships_target_fact_id", table_name="loop_fact_relationships")
    op.drop_index("ix_loop_fact_relationships_source_fact_id", table_name="loop_fact_relationships")
    op.drop_index("ix_loop_fact_relationships_loop_id", table_name="loop_fact_relationships")
    op.drop_table("loop_fact_relationships")
    op.drop_index("ix_loop_fact_revisions_loop_id", table_name="loop_fact_revisions")
    op.drop_index("ix_loop_fact_revisions_fact_id", table_name="loop_fact_revisions")
    op.drop_table("loop_fact_revisions")
    op.drop_index("ix_loop_fact_current_subject", table_name="loop_facts")
    op.drop_index("ix_loop_facts_correlation_id", table_name="loop_facts")
    op.drop_index("ix_loop_facts_source_run_id", table_name="loop_facts")
    op.drop_index("ix_loop_facts_source_context_id", table_name="loop_facts")
    op.drop_index("ix_loop_facts_fact_type", table_name="loop_facts")
    op.drop_index("ix_loop_facts_loop_id", table_name="loop_facts")
    op.drop_table("loop_facts")
