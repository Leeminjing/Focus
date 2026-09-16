r"""本文件对外提供持续多 Context 策展的 SQLAlchemy 持久化实体与状态枚举。

输入为 Program、来源订阅、稳定 Lane、Portfolio revision、Lane candidate、准备/发布 attempt 与
outbox 事实；输出为分表 ORM 模型、单发布者约束及可审计状态。具体工作流为 Program 聚合多来源
和多 Lane，Portfolio revision 冻结一代候选与控制版本，publication attempt/outbox 记录原子切换结果，
partial unique index 保证每个 managed Context 只有一个未释放发布者。
示例：`lane = CurationLane(lane_id="l1", program_id="p1", purpose="testing", ...)`。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.desktop.context_evolution import models as _context_revision_models
from focus.persistence.base import Base


class CurationProgramControlState(StrEnum):
    FOLLOWING = "following"
    PAUSED = "paused"
    STOPPED = "stopped"


class CurationLaneLifecycle(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class PortfolioRevisionStatus(StrEnum):
    OBSERVED = "observed"
    DECIDING = "deciding"
    WAITING_WORKERS = "waiting_workers"
    PREPARING = "preparing"
    READY = "ready"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    DEGRADED = "degraded"
    WAITING_USER = "waiting_user"
    ERROR = "error"


class PortfolioLaneAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    KEEP = "keep"
    PAUSE = "pause"
    RETIRE = "retire"


class PortfolioLaneCandidateStatus(StrEnum):
    PENDING = "pending"
    PREPARING = "preparing"
    READY = "ready"
    PUBLISHED = "published"
    UNCHANGED = "unchanged"
    ERROR = "error"
    SUPERSEDED = "superseded"


class CurationWorkerKind(StrEnum):
    LANE_CURATOR = "lane_curator"
    LEGACY_CURATOR = "legacy_curator"


class CurationAttemptStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    SUPERSEDED = "superseded"


class PortfolioPublicationAttemptStatus(StrEnum):
    PREPARING = "preparing"
    READY = "ready"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    ERROR = "error"
    SUPERSEDED = "superseded"
    RECOVERY_REQUIRED = "recovery_required"


class CurationOutboxStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    DELIVERED = "delivered"
    ERROR = "error"


class CurationProgram(Base):
    __tablename__ = "curation_programs"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="ck_curation_program_revision_nonnegative"),
        CheckConstraint(
            "control_state IN ('following', 'paused', 'stopped')",
            name="ck_curation_program_control_state",
        ),
    )

    program_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    patrol_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("patrol_agents.agent_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    control_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="following", server_default="following"
    )
    policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    current_portfolio_revision_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey(
            "curation_portfolio_revisions.portfolio_revision_id",
            name="fk_curation_program_current_portfolio",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CurationSourceSubscription(Base):
    __tablename__ = "curation_source_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "program_id", "source_context_id", name="uq_curation_subscription_source"
        ),
        UniqueConstraint(
            "program_id", "position", name="uq_curation_subscription_position"
        ),
        CheckConstraint("position >= 0", name="ck_curation_subscription_position"),
    )

    subscription_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    program_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("curation_programs.program_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_context_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_role: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CurationLane(Base):
    __tablename__ = "curation_lanes"
    __table_args__ = (
        UniqueConstraint("program_id", "normalized_purpose", name="uq_curation_lane_purpose"),
        CheckConstraint("publisher_epoch > 0", name="ck_curation_lane_publisher_epoch_positive"),
        CheckConstraint(
            "lifecycle IN ('active', 'paused', 'retired')",
            name="ck_curation_lane_lifecycle",
        ),
        Index(
            "uq_curation_lane_managed_publisher",
            "managed_context_id",
            unique=True,
            postgresql_where=text(
                "managed_context_id IS NOT NULL AND lifecycle <> 'retired'"
            ),
        ),
    )

    lane_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    program_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("curation_programs.program_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    managed_context_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("desktop_threads.task_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_purpose: Mapped[str] = mapped_column(String(160), nullable=False)
    lane_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    lifecycle: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    publisher_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    current_source_frontier_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_semantic_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PortfolioRevision(Base):
    __tablename__ = "curation_portfolio_revisions"
    __table_args__ = (
        UniqueConstraint("program_id", "generation", name="uq_curation_portfolio_generation"),
        CheckConstraint("generation > 0", name="ck_curation_portfolio_generation_positive"),
        CheckConstraint(
            "base_program_revision >= 0",
            name="ck_curation_portfolio_base_revision_nonnegative",
        ),
        CheckConstraint(
            "status IN ('observed', 'deciding', 'waiting_workers', 'preparing', 'ready', "
            "'publishing', 'published', 'superseded', 'degraded', 'waiting_user', 'error')",
            name="ck_curation_portfolio_status",
        ),
    )

    portfolio_revision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    program_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("curation_programs.program_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    source_frontier: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    frontier_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_program_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    control_revisions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    target_lanes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="observed", server_default="observed"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PortfolioLaneCandidate(Base):
    __tablename__ = "curation_portfolio_lane_candidates"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_revision_id", "lane_id", name="uq_curation_candidate_lane"
        ),
        CheckConstraint(
            "action IN ('create', 'update', 'keep', 'pause', 'retire')",
            name="ck_curation_candidate_action",
        ),
        CheckConstraint(
            "status IN ('pending', 'preparing', 'ready', 'published', 'unchanged', "
            "'error', 'superseded')",
            name="ck_curation_candidate_status",
        ),
        CheckConstraint(
            "base_publisher_epoch > 0",
            name="ck_curation_candidate_base_publisher_epoch_positive",
        ),
    )

    candidate_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    portfolio_revision_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("curation_portfolio_revisions.portfolio_revision_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lane_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("curation_lanes.lane_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    target_context_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    base_publisher_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    base_context_revision_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("desktop_context_revisions.revision_id", ondelete="RESTRICT"),
        nullable=True,
    )
    candidate_context_revision_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("desktop_context_revisions.revision_id", ondelete="RESTRICT"),
        nullable=True,
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    source_allocation: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    source_frontier_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    semantic_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_lineage: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    source_dispositions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CurationAttempt(Base):
    __tablename__ = "curation_attempts"
    __table_args__ = (
        UniqueConstraint("candidate_id", "attempt_number", name="uq_curation_attempt_number"),
        CheckConstraint("attempt_number > 0", name="ck_curation_attempt_number_positive"),
        CheckConstraint(
            "worker_kind IN ('lane_curator', 'legacy_curator')",
            name="ck_curation_attempt_worker_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'error', 'interrupted', 'superseded')",
            name="ck_curation_attempt_status",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("curation_portfolio_lane_candidates.candidate_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    raw_output: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    parsed_output: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PortfolioPublicationAttempt(Base):
    __tablename__ = "curation_portfolio_publication_attempts"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_revision_id",
            "attempt_number",
            name="uq_curation_publication_attempt_number",
        ),
        CheckConstraint("attempt_number > 0", name="ck_curation_publication_attempt_positive"),
        CheckConstraint(
            "status IN ('preparing', 'ready', 'publishing', 'published', 'error', "
            "'superseded', 'recovery_required')",
            name="ck_curation_publication_attempt_status",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    portfolio_revision_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("curation_portfolio_revisions.portfolio_revision_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CurationOutboxEvent(Base):
    __tablename__ = "curation_outbox_events"
    __table_args__ = (
        UniqueConstraint(
            "aggregate_id", "event_type", name="uq_curation_outbox_aggregate_event"
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'delivered', 'error')",
            name="ck_curation_outbox_status",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    aggregate_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    delivery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    claimed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CurationOutboxDelivery(Base):
    __tablename__ = "curation_outbox_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "consumer_id", name="uq_curation_outbox_delivery"),
    )

    delivery_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("curation_outbox_events.event_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    consumer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
