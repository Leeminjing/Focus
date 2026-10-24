"""Durable, immutable work history. Source IDs deliberately survive journal retention.

Entries are a read projection, separate from current facts. Business timestamps
are optional: recorded_at never pretends to be the time the underlying work ran.
"""
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class WorkTimelineEntry(Base):
    __tablename__ = "work_timeline_entries"
    __table_args__ = (
        UniqueConstraint("loop_id", "source_event_id", name="uq_work_timeline_source"),
        UniqueConstraint("loop_id", "sequence", name="uq_work_timeline_sequence"),
        Index("ix_work_timeline_page", "loop_id", "sequence"),
        Index("ix_work_timeline_round", "loop_id", "round_id", "sequence"),
    )

    entry_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(String(2000), nullable=False)
    round_id: Mapped[str | None] = mapped_column(String(32))
    round_number: Mapped[int | None] = mapped_column(Integer)
    context_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    run_id: Mapped[str | None] = mapped_column(String(32))
    fact_id: Mapped[str | None] = mapped_column(String(32))
    fact_revision: Mapped[int | None] = mapped_column(Integer)
    intent_id: Mapped[str | None] = mapped_column(String(32))
    decision_id: Mapped[str | None] = mapped_column(String(32))
    correlation_id: Mapped[str | None] = mapped_column(String(120))
    causation_id: Mapped[str | None] = mapped_column(String(64))
    references: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    visibility: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
