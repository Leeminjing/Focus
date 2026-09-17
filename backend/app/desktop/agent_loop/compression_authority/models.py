r"""本文件对外提供 LoopCompressionCandidate 与 LoopCompressionResolution ORM 事实。

输入为精确 Loop/grant/round/pending/Context revision 身份、规范化 ranges 和恢复结果；输出为可并发
锁定、可审计、可重放的候选与 resolution 行。具体工作流为候选保持非权威，Kernel 原子接受一个候选
并创建唯一 resolution，恢复器推进 committed→resuming→applied 或终止态。示例：
`candidate.status = "accepted"; resolution.status = "committed"`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class LoopCompressionCandidate(Base):
    __tablename__ = "loop_compression_candidates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('prepared','accepted','rejected','superseded','expired')",
            name="ck_loop_compression_candidate_status",
        ),
        UniqueConstraint("candidate_hash", name="uq_loop_compression_candidate_hash"),
        Index(
            "uq_loop_compression_live_candidate",
            "pending_decision_id",
            unique=True,
            postgresql_where=text("status IN ('prepared','accepted')"),
        ),
        Index("ix_loop_compression_candidate_loop_created", "loop_id", "created_at"),
    )

    candidate_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False)
    pending_decision_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_pending_decisions.pending_decision_id", ondelete="CASCADE"), nullable=False)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False)
    context_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"), nullable=False)
    base_context_revision_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_context_revisions.revision_id", ondelete="RESTRICT"), nullable=False)
    base_checkpoint_id: Mapped[str] = mapped_column(Text, nullable=False)
    frontier_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    authority_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    goal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    normalized_ranges: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    replacement_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    before_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    after_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    protection_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared", server_default="prepared")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoopCompressionResolution(Base):
    __tablename__ = "loop_compression_resolutions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('committed','resuming','applied','superseded','failed')",
            name="ck_loop_compression_resolution_status",
        ),
        UniqueConstraint("pending_decision_id", name="uq_loop_compression_resolution_pending"),
        UniqueConstraint("candidate_id", name="uq_loop_compression_resolution_candidate"),
        UniqueConstraint("idempotency_key", name="uq_loop_compression_resolution_idempotency"),
        Index("ix_loop_compression_resolution_status_lease", "status", "lease_expires_at"),
    )

    resolution_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False)
    pending_decision_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_pending_decisions.pending_decision_id", ondelete="CASCADE"), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_compression_candidates.candidate_id", ondelete="RESTRICT"), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_decisions.decision_id", ondelete="CASCADE"), nullable=False)
    action_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_actions.action_id", ondelete="CASCADE"), nullable=False)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False)
    grant_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_delegation_grants.grant_id", ondelete="RESTRICT"), nullable=False)
    grant_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    goal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    resume_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="committed", server_default="committed")
    resume_run_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("desktop_runs.run_id", ondelete="SET NULL"), nullable=True)
    result_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_context_revision_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("desktop_context_revisions.revision_id", ondelete="SET NULL"), nullable=True)
    actual_before_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_after_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
