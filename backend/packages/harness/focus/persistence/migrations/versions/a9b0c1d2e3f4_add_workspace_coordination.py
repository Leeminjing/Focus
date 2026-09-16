r"""本文件对外提供 Workspace Slot、lease、execution anchor、adoption 与 tombstone 可逆迁移。

输入为统一 Run revision `f8a9b0c1d2e3`；输出为单 writer fencing、可版本化物理 workspace、
隔离结果采用及安全清理所需表与约束。具体工作流为先建 slot，再按依赖顺序建立 lease、anchor、
adoption 和 tombstone。示例：`alembic upgrade a9b0c1d2e3f4`。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_object = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "workspace_slots",
        sa.Column("slot_id", sa.String(32), primary_key=True),
        sa.Column("workspace_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(32), server_default="local", nullable=False),
        sa.Column("base_revision", sa.String(64)),
        sa.Column("current_fingerprint", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("fencing_counter", sa.Integer(), server_default="0", nullable=False),
        sa.Column("owner_loop_id", sa.String(32)),
        sa.Column("owner_lane_id", sa.String(32)),
        sa.Column("lifecycle", sa.String(16), server_default="active", nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", json_object, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint("kind IN ('authoritative', 'isolated')", name="ck_workspace_slot_kind"),
        sa.CheckConstraint("lifecycle IN ('active', 'retained', 'released', 'deleted')", name="ck_workspace_slot_lifecycle"),
        sa.CheckConstraint("fencing_counter >= 0", name="ck_workspace_slot_fencing_counter"),
        sa.ForeignKeyConstraint(["workspace_id"], ["desktop_workspaces.workspace_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_workspace_slots_workspace_id", "workspace_slots", ["workspace_id"])
    op.create_index("ix_workspace_slots_owner_loop_id", "workspace_slots", ["owner_loop_id"])
    op.create_index("ix_workspace_slots_owner_lane_id", "workspace_slots", ["owner_lane_id"])
    op.create_index("uq_workspace_authoritative_slot", "workspace_slots", ["workspace_id"], unique=True, postgresql_where=sa.text("kind = 'authoritative' AND lifecycle <> 'deleted'"))
    op.create_table(
        "workspace_leases",
        sa.Column("lease_id", sa.String(32), primary_key=True),
        sa.Column("slot_id", sa.String(32), nullable=False),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint("mode IN ('read', 'write')", name="ck_workspace_lease_mode"),
        sa.CheckConstraint("status IN ('active', 'released', 'expired', 'fenced')", name="ck_workspace_lease_status"),
        sa.CheckConstraint("fencing_token > 0", name="ck_workspace_lease_fencing_positive"),
        sa.ForeignKeyConstraint(["slot_id"], ["workspace_slots.slot_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["desktop_runs.run_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", name="uq_workspace_lease_run"),
    )
    op.create_index("ix_workspace_leases_slot_id", "workspace_leases", ["slot_id"])
    op.create_index("ix_workspace_leases_run_id", "workspace_leases", ["run_id"])
    op.create_index("uq_workspace_active_writer", "workspace_leases", ["slot_id"], unique=True, postgresql_where=sa.text("mode = 'write' AND status = 'active'"))
    op.create_table(
        "run_execution_anchors",
        sa.Column("run_id", sa.String(32), primary_key=True),
        sa.Column("context_revision_id", sa.String(32)),
        sa.Column("checkpoint_id", sa.Text()),
        sa.Column("slot_id", sa.String(32), nullable=False),
        sa.Column("lease_id", sa.String(32)),
        sa.Column("directive_id", sa.String(32)),
        sa.Column("observed_workspace_revision", sa.Integer(), nullable=False),
        sa.Column("observed_fingerprint", sa.String(64), nullable=False),
        sa.Column("resulting_workspace_revision", sa.Integer()),
        sa.Column("resulting_fingerprint", sa.String(64)),
        sa.Column("effect_evidence", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("adoption_state", sa.String(24), server_default="not_required", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["run_id"], ["desktop_runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["context_revision_id"], ["desktop_context_revisions.revision_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["slot_id"], ["workspace_slots.slot_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["lease_id"], ["workspace_leases.lease_id"], ondelete="SET NULL"),
    )
    op.create_index("ix_run_execution_anchors_slot_id", "run_execution_anchors", ["slot_id"])
    op.create_index("ix_run_execution_anchors_directive_id", "run_execution_anchors", ["directive_id"])
    op.create_table(
        "workspace_adoptions",
        sa.Column("adoption_id", sa.String(32), primary_key=True),
        sa.Column("source_slot_id", sa.String(32), nullable=False),
        sa.Column("target_slot_id", sa.String(32), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("expected_target_revision", sa.Integer(), nullable=False),
        sa.Column("resulting_target_revision", sa.Integer()),
        sa.Column("status", sa.String(24), server_default="pending", nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("conflict", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending', 'adopting', 'adopted', 'conflict', 'waiting_user', 'rejected')", name="ck_workspace_adoption_status"),
        sa.ForeignKeyConstraint(["source_slot_id"], ["workspace_slots.slot_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_slot_id"], ["workspace_slots.slot_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("source_slot_id", "source_revision", name="uq_workspace_adoption_source_revision"),
    )
    op.create_table(
        "workspace_slot_tombstones",
        sa.Column("tombstone_id", sa.String(32), primary_key=True),
        sa.Column("slot_id", sa.String(32), nullable=False, unique=True),
        sa.Column("workspace_id", sa.String(32), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("final_fingerprint", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_workspace_slot_tombstones_workspace_id", "workspace_slot_tombstones", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_workspace_slot_tombstones_workspace_id", table_name="workspace_slot_tombstones")
    op.drop_table("workspace_slot_tombstones")
    op.drop_table("workspace_adoptions")
    op.drop_index("ix_run_execution_anchors_directive_id", table_name="run_execution_anchors")
    op.drop_index("ix_run_execution_anchors_slot_id", table_name="run_execution_anchors")
    op.drop_table("run_execution_anchors")
    op.drop_index("uq_workspace_active_writer", table_name="workspace_leases")
    op.drop_index("ix_workspace_leases_run_id", table_name="workspace_leases")
    op.drop_index("ix_workspace_leases_slot_id", table_name="workspace_leases")
    op.drop_table("workspace_leases")
    op.drop_index("uq_workspace_authoritative_slot", table_name="workspace_slots")
    op.drop_index("ix_workspace_slots_owner_lane_id", table_name="workspace_slots")
    op.drop_index("ix_workspace_slots_owner_loop_id", table_name="workspace_slots")
    op.drop_index("ix_workspace_slots_workspace_id", table_name="workspace_slots")
    op.drop_table("workspace_slots")
