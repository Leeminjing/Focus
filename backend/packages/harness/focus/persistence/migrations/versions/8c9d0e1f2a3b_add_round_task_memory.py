"""本文件对外提供任务记忆、领域来源和 Context 发布证明的 additive 迁移。

输入为上一版数据库；输出为独立版本、工作、吸收、冻结输入及领域结果表。
具体工作流为建表和索引，发布证明仅从当前已发布根及可达历史回填；不改旧 Observation 或 Fact。
示例：alembic upgrade head；downgrade 仅撤销本迁移创建的表。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "8c9d0e1f2a3b"
down_revision = "7b8c9d0e1f2a"
branch_labels = None
depends_on = None


def _loop_column():
    return sa.Column(
        "loop_id",
        sa.String(32),
        sa.ForeignKey("agent_loops.loop_id", ondelete="CASCADE"),
        nullable=False,
    )


def _created_column():
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def upgrade():
    op.create_table(
        "loop_task_progress",
        sa.Column("progress_id", sa.String(32), primary_key=True),
        _loop_column(),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "observation_id",
            sa.String(32),
            sa.ForeignKey("loop_observations.observation_id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "previous_progress_id",
            sa.String(32),
            sa.ForeignKey("loop_task_progress.progress_id", ondelete="RESTRICT"),
        ),
        sa.Column("document", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("contribution", JSONB(), nullable=False),
        _created_column(),
        sa.UniqueConstraint(
            "loop_id", "generation", name="uq_task_progress_generation"
        ),
        sa.UniqueConstraint("observation_id", name="uq_task_progress_observation"),
    )
    op.create_index("ix_loop_task_progress_loop_id", "loop_task_progress", ["loop_id"])
    op.create_table(
        "loop_progress_heads",
        _loop_column(),
        sa.Column(
            "progress_id",
            sa.String(32),
            sa.ForeignKey("loop_task_progress.progress_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("loop_id"),
    )
    op.create_table(
        "loop_decision_inputs",
        sa.Column(
            "observation_id",
            sa.String(32),
            sa.ForeignKey("loop_observations.observation_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        _loop_column(),
        sa.Column(
            "round_id",
            sa.String(32),
            sa.ForeignKey("loop_rounds.round_id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
    )
    op.create_index(
        "ix_loop_decision_inputs_loop_id", "loop_decision_inputs", ["loop_id"]
    )
    op.create_table(
        "loop_progress_work",
        sa.Column(
            "observation_id",
            sa.String(32),
            sa.ForeignKey("loop_decision_inputs.observation_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        _loop_column(),
        sa.Column("state", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("fence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.Column("usage", JSONB(), nullable=False, server_default="[]"),
        sa.Column("retry_budget_authorization", JSONB()),
        sa.Column("attempt_events", JSONB(), nullable=False, server_default="[]"),
        _created_column(),
    )
    op.create_index("ix_loop_progress_work_loop_id", "loop_progress_work", ["loop_id"])
    op.create_table(
        "loop_decision_supplements",
        sa.Column("supplement_id", sa.String(64), primary_key=True),
        sa.Column(
            "observation_id",
            sa.String(32),
            sa.ForeignKey("loop_observations.observation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.UniqueConstraint(
            "observation_id", "kind", name="uq_decision_supplement_kind"
        ),
    )
    op.create_table(
        "loop_progress_receipts",
        _loop_column(),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column(
            "progress_id",
            sa.String(32),
            sa.ForeignKey("loop_task_progress.progress_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("loop_id", "source_key"),
    )
    op.create_table(
        "desktop_domain_results",
        sa.Column("result_key", sa.String(64), primary_key=True),
        sa.Column(
            "loop_id",
            sa.String(32),
            sa.ForeignKey("agent_loops.loop_id", ondelete="CASCADE"),
        ),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("source_id", sa.String(160), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column(
            "context_id",
            sa.String(32),
            sa.ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "run_id",
            sa.String(32),
            sa.ForeignKey("desktop_runs.run_id", ondelete="RESTRICT"),
        ),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("audit", JSONB(), nullable=False),
        _created_column(),
        sa.UniqueConstraint(
            "kind", "source_id", "version", name="uq_domain_result_version"
        ),
    )
    op.create_index(
        "ix_desktop_domain_results_loop_id", "desktop_domain_results", ["loop_id"]
    )
    op.create_index(
        "ix_desktop_domain_results_created_at", "desktop_domain_results", ["created_at"]
    )
    op.create_table(
        "desktop_context_publications",
        sa.Column(
            "revision_id",
            sa.String(32),
            sa.ForeignKey("desktop_context_revisions.revision_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "context_id",
            sa.String(32),
            sa.ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        _created_column(),
    )
    op.create_index(
        "ix_desktop_context_publications_context_id",
        "desktop_context_publications",
        ["context_id"],
    )
    op.execute("""WITH RECURSIVE published(revision_id) AS (
        SELECT current_revision_id FROM desktop_threads WHERE current_revision_id IS NOT NULL
        UNION SELECT s.source_revision_id FROM desktop_context_revision_sources s JOIN published p ON s.target_revision_id = p.revision_id
    ) INSERT INTO desktop_context_publications(revision_id, context_id)
    SELECT r.revision_id, r.context_id FROM desktop_context_revisions r JOIN published p ON p.revision_id = r.revision_id
    WHERE r.projection_status IN ('valid', 'repaired', 'approved', 'deleted') ON CONFLICT DO NOTHING""")


def downgrade():
    for table in (
        "desktop_context_publications",
        "desktop_domain_results",
        "loop_progress_receipts",
        "loop_decision_supplements",
        "loop_progress_work",
        "loop_decision_inputs",
        "loop_progress_heads",
        "loop_task_progress",
    ):
        op.drop_table(table)
