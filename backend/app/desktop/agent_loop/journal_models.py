r"""本文件对外提供 LoopJournalSequence、LoopJournalEvent、LoopReplayRetention 与 LoopProjectorCursor 持久化实体。

输入为 Loop identity、规范事件 envelope、保留边界和 projector 进度；输出为每 Loop 严格递增的可重放事件日志。
具体工作流为 sequence 行串行分配提交序号，journal event 保存版本化安全 payload，retention 标记最早可重放位置，
cursor 记录各投影器消费边界。示例：`session.add(LoopJournalEvent(loop_id="l1", sequence=1, ...))`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class LoopJournalSequence(Base):
    __tablename__ = "loop_journal_sequences"

    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), primary_key=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LoopJournalEvent(Base):
    __tablename__ = "loop_journal_events"
    __table_args__ = (
        UniqueConstraint("loop_id", "sequence", name="uq_loop_journal_sequence"),
        UniqueConstraint("loop_id", "idempotency_key", name="uq_loop_journal_idempotency"),
        UniqueConstraint("loop_id", "kind", "entity_type", "entity_id", "entity_revision", name="uq_loop_journal_entity_transition"),
        Index("ix_loop_journal_replay", "loop_id", "sequence"),
    )

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    kind: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    visibility: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    retained_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoopReplayRetention(Base):
    __tablename__ = "loop_replay_retention"

    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), primary_key=True)
    minimum_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    retention_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=604800, server_default="604800")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LoopProjectorCursor(Base):
    __tablename__ = "loop_projector_cursors"

    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), primary_key=True)
    projector_name: Mapped[str] = mapped_column(String(80), primary_key=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
