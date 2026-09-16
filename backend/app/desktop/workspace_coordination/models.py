r"""本文件对外提供 WorkspaceSlot、WorkspaceLease、RunExecutionAnchor 与 WorkspaceAdoption ORM 实体。

输入为可信 workspace、Run、Loop、Lane、fingerprint 与租约事实；输出为可锁定的 slot、单调 fencing
token、执行锚点、采用记录和清理墓碑。具体工作流为 slot 维护物理世界版本，lease 约束活动 writer，
anchor 固化 Run 前后状态，adoption 原子记录隔离结果进入权威 slot。示例：
`slot = WorkspaceSlot(slot_id="s1", kind="authoritative", ...)`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class WorkspaceSlot(Base):
    __tablename__ = "workspace_slots"
    __table_args__ = (
        CheckConstraint("kind IN ('authoritative', 'isolated')", name="ck_workspace_slot_kind"),
        CheckConstraint("lifecycle IN ('active', 'retained', 'released', 'deleted')", name="ck_workspace_slot_lifecycle"),
        CheckConstraint("fencing_counter >= 0", name="ck_workspace_slot_fencing_counter"),
        Index("uq_workspace_authoritative_slot", "workspace_id", unique=True, postgresql_where=text("kind = 'authoritative' AND lifecycle <> 'deleted'")),
    )

    slot_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_workspaces.workspace_id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="local", server_default="local")
    base_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    fencing_counter: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    owner_loop_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    owner_lane_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WorkspaceLease(Base):
    __tablename__ = "workspace_leases"
    __table_args__ = (
        CheckConstraint("mode IN ('read', 'write')", name="ck_workspace_lease_mode"),
        CheckConstraint("status IN ('active', 'released', 'expired', 'fenced')", name="ck_workspace_lease_status"),
        CheckConstraint("fencing_token > 0", name="ck_workspace_lease_fencing_positive"),
        UniqueConstraint("run_id", name="uq_workspace_lease_run"),
        Index("uq_workspace_active_writer", "slot_id", unique=True, postgresql_where=text("mode = 'write' AND status = 'active'")),
    )

    lease_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    slot_id: Mapped[str] = mapped_column(String(32), ForeignKey("workspace_slots.slot_id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_runs.run_id", ondelete="CASCADE"), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunExecutionAnchor(Base):
    __tablename__ = "run_execution_anchors"

    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_runs.run_id", ondelete="CASCADE"), primary_key=True)
    context_revision_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("desktop_context_revisions.revision_id", ondelete="RESTRICT"), nullable=True)
    checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    slot_id: Mapped[str] = mapped_column(String(32), ForeignKey("workspace_slots.slot_id", ondelete="RESTRICT"), nullable=False, index=True)
    lease_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("workspace_leases.lease_id", ondelete="SET NULL"), nullable=True)
    directive_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    observed_workspace_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resulting_workspace_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resulting_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effect_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    adoption_state: Mapped[str] = mapped_column(String(24), nullable=False, default="not_required", server_default="not_required")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceAdoption(Base):
    __tablename__ = "workspace_adoptions"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'adopting', 'adopted', 'conflict', 'waiting_user', 'rejected')", name="ck_workspace_adoption_status"),
        UniqueConstraint("source_slot_id", "source_revision", name="uq_workspace_adoption_source_revision"),
    )

    adoption_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_slot_id: Mapped[str] = mapped_column(String(32), ForeignKey("workspace_slots.slot_id", ondelete="RESTRICT"), nullable=False)
    target_slot_id: Mapped[str] = mapped_column(String(32), ForeignKey("workspace_slots.slot_id", ondelete="RESTRICT"), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_target_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_target_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    conflict: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceSlotTombstone(Base):
    __tablename__ = "workspace_slot_tombstones"

    tombstone_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    slot_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    workspace_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    final_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
