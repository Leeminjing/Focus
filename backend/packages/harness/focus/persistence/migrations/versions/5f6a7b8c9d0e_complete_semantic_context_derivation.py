r"""本迁移对外提供完整 semantic Context 派生的 index、retrieval session 与阶段 artifact 持久化结构。

输入为 semantic Context derivation 已存在的数据库；输出为三个追加式审计表及检索索引。具体工作流为创建 immutable Revision
index cache、mutable-but-frozen-scope planning session 和按 stage/input/version 唯一的 derivation artifact；downgrade 仅逆序删除
新表，不重写已提交 Context lineage。示例：`alembic upgrade 5f6a7b8c9d0e`。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "5f6a7b8c9d0e"
down_revision = "4e5f6a7b8c9d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loop_semantic_index_artifacts",
        sa.Column("index_id", sa.String(length=64), primary_key=True),
        sa.Column("context_id", sa.String(length=32), sa.ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision_id", sa.String(length=32), sa.ForeignKey("desktop_context_revisions.revision_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("index_schema_version", sa.String(length=64), nullable=False),
        sa.Column("segmenter_version", sa.String(length=64), nullable=False),
        sa.Column("projector_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="ready", nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("attempt_records", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "revision_id",
            "source_content_hash",
            "index_schema_version",
            "segmenter_version",
            "projector_version",
            name="uq_loop_semantic_index_cache_key",
        ),
    )
    op.create_index("ix_loop_semantic_index_context_id", "loop_semantic_index_artifacts", ["context_id"])
    op.create_index("ix_loop_semantic_index_revision_id", "loop_semantic_index_artifacts", ["revision_id"])
    op.create_table(
        "loop_planning_retrieval_sessions",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        sa.Column("loop_id", sa.String(length=32), sa.ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False),
        sa.Column("round_id", sa.String(length=32), sa.ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False),
        sa.Column("frontier_hash", sa.String(length=64), nullable=False),
        sa.Column("catalog_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("budget", postgresql.JSONB(), nullable=False),
        sa.Column("usage", postgresql.JSONB(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("attempt_records", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_loop_planning_session_loop_id", "loop_planning_retrieval_sessions", ["loop_id"])
    op.create_index("ix_loop_planning_session_round_id", "loop_planning_retrieval_sessions", ["round_id"])
    op.create_table(
        "loop_context_derivation_artifacts",
        sa.Column("artifact_id", sa.String(length=64), primary_key=True),
        sa.Column("loop_id", sa.String(length=32), sa.ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False),
        sa.Column("round_id", sa.String(length=32), sa.ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False),
        sa.Column("expansion_id", sa.String(length=64), sa.ForeignKey("loop_context_expansions.expansion_id", ondelete="CASCADE"), nullable=True),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("input_identity", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("attempt_records", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "loop_id",
            "round_id",
            "stage",
            "input_identity",
            "version",
            name="uq_loop_context_derivation_stage_input",
        ),
    )
    op.create_index("ix_loop_derivation_artifact_loop_id", "loop_context_derivation_artifacts", ["loop_id"])
    op.create_index("ix_loop_derivation_artifact_round_id", "loop_context_derivation_artifacts", ["round_id"])
    op.create_index("ix_loop_derivation_artifact_expansion_id", "loop_context_derivation_artifacts", ["expansion_id"])


def downgrade() -> None:
    op.execute("UPDATE loop_context_expansions SET state = 'signals_collected' WHERE state = 'indexes_ready'")
    op.execute("UPDATE loop_context_expansions SET state = 'portfolio_projected' WHERE state = 'retrieval_planned'")
    op.execute("UPDATE loop_context_expansions SET state = 'work_planned' WHERE state = 'work_reconciled'")
    op.execute("UPDATE loop_context_expansions SET state = 'dossier_built' WHERE state = 'quality_verified'")
    op.drop_index("ix_loop_derivation_artifact_expansion_id", table_name="loop_context_derivation_artifacts")
    op.drop_index("ix_loop_derivation_artifact_round_id", table_name="loop_context_derivation_artifacts")
    op.drop_index("ix_loop_derivation_artifact_loop_id", table_name="loop_context_derivation_artifacts")
    op.drop_table("loop_context_derivation_artifacts")
    op.drop_index("ix_loop_planning_session_round_id", table_name="loop_planning_retrieval_sessions")
    op.drop_index("ix_loop_planning_session_loop_id", table_name="loop_planning_retrieval_sessions")
    op.drop_table("loop_planning_retrieval_sessions")
    op.drop_index("ix_loop_semantic_index_revision_id", table_name="loop_semantic_index_artifacts")
    op.drop_index("ix_loop_semantic_index_context_id", table_name="loop_semantic_index_artifacts")
    op.drop_table("loop_semantic_index_artifacts")
