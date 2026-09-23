r"""本迁移对外提供 semantic Context derivation 的冻结合同、frontier、stage identity 与 compiled-plan 持久化字段。

输入为既有 loop_context_expansions 表；输出为 work spec、manifest/source/evidence frontier、resolution、版本和最终计划列及查询索引。
具体工作流为追加 nullable/JSONB 列、从旧 opportunity 回填可恢复字段并映射活动状态；downgrade 先恢复旧状态/source 再逆序删除。
示例：`alembic upgrade 4e5f6a7b8c9d`。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "4e5f6a7b8c9d"
down_revision = "3d4e5f6a7b8c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("loop_context_expansions", sa.Column("work_spec", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column("loop_context_expansions", sa.Column("manifest_ids", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.add_column("loop_context_expansions", sa.Column("source_frontier", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.add_column("loop_context_expansions", sa.Column("resolution", postgresql.JSONB(), nullable=True))
    op.add_column("loop_context_expansions", sa.Column("evidence_frontier", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.add_column("loop_context_expansions", sa.Column("stage_identities", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column("loop_context_expansions", sa.Column("compiled_plan", postgresql.JSONB(), nullable=True))
    op.add_column("loop_context_expansions", sa.Column("definition_hash", sa.String(length=64), nullable=True))
    op.add_column("loop_context_expansions", sa.Column("planner_version", sa.String(length=64), nullable=True))
    op.add_column("loop_context_expansions", sa.Column("projector_version", sa.String(length=64), nullable=True))
    op.add_column("loop_context_expansions", sa.Column("resolver_version", sa.String(length=64), nullable=True))
    op.alter_column("loop_context_expansions", "source_context_id", existing_type=sa.String(length=32), nullable=True)
    op.alter_column("loop_context_expansions", "source_revision_id", existing_type=sa.String(length=32), nullable=True)
    op.execute("UPDATE loop_context_expansions SET work_spec = COALESCE(opportunity->'work_spec', '{}'::jsonb)")
    op.execute("UPDATE loop_context_expansions SET manifest_ids = COALESCE(opportunity->'manifest_ids', '[]'::jsonb)")
    op.execute("UPDATE loop_context_expansions SET source_frontier = CASE WHEN opportunity ? 'manifest_sources' THEN opportunity->'manifest_sources' WHEN opportunity ? 'source' THEN jsonb_build_array(opportunity->'source') ELSE '[]'::jsonb END")
    op.execute("UPDATE loop_context_expansions SET planner_version = opportunity#>>'{work_spec,planner_version}'")
    op.execute("UPDATE loop_context_expansions SET stage_identities = jsonb_build_object('work_spec_id', opportunity#>>'{work_spec,work_spec_id}', 'observation_hash', opportunity->>'observation_hash', 'manifest_ids', manifest_ids)")
    op.execute("UPDATE loop_context_expansions SET state = 'admitted' WHERE state IN ('detected', 'curated')")
    op.alter_column("loop_context_expansions", "state", existing_type=sa.String(length=24), server_default="signals_collected")
    op.execute("CREATE INDEX ix_loop_context_expansions_work_spec_id ON loop_context_expansions ((work_spec->>'work_spec_id'))")
    op.execute("CREATE INDEX ix_loop_context_expansions_resolution_id ON loop_context_expansions ((resolution->>'resolution_id'))")
    op.create_index("ix_loop_context_expansions_definition_hash", "loop_context_expansions", ["definition_hash"])


def downgrade() -> None:
    op.execute("UPDATE loop_context_expansions SET state = 'detected' WHERE state IN ('signals_collected', 'portfolio_projected', 'work_planned', 'admitted')")
    op.execute("UPDATE loop_context_expansions SET state = 'proposed' WHERE state IN ('evidence_resolved', 'dossier_built', 'synthesis_omitted')")
    op.execute("UPDATE loop_context_expansions SET source_context_id = COALESCE(source_context_id, source_frontier->0->>'context_id'), source_revision_id = COALESCE(source_revision_id, source_frontier->0->>'revision_id')")
    op.alter_column("loop_context_expansions", "state", existing_type=sa.String(length=24), server_default="detected")
    op.alter_column("loop_context_expansions", "source_revision_id", existing_type=sa.String(length=32), nullable=False)
    op.alter_column("loop_context_expansions", "source_context_id", existing_type=sa.String(length=32), nullable=False)
    op.drop_index("ix_loop_context_expansions_definition_hash", table_name="loop_context_expansions")
    op.drop_index("ix_loop_context_expansions_resolution_id", table_name="loop_context_expansions")
    op.drop_index("ix_loop_context_expansions_work_spec_id", table_name="loop_context_expansions")
    op.drop_column("loop_context_expansions", "resolver_version")
    op.drop_column("loop_context_expansions", "projector_version")
    op.drop_column("loop_context_expansions", "planner_version")
    op.drop_column("loop_context_expansions", "definition_hash")
    op.drop_column("loop_context_expansions", "compiled_plan")
    op.drop_column("loop_context_expansions", "stage_identities")
    op.drop_column("loop_context_expansions", "evidence_frontier")
    op.drop_column("loop_context_expansions", "resolution")
    op.drop_column("loop_context_expansions", "source_frontier")
    op.drop_column("loop_context_expansions", "manifest_ids")
    op.drop_column("loop_context_expansions", "work_spec")
