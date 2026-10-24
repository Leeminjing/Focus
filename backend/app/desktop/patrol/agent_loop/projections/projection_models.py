r"""本文件对外提供 LoopProjectionFailure 与 LoopProjectionUnitOutcome 持久化实体。

输入为 projector 名称、稳定投影单元、journal 边界、错误分类和处理结果；输出为幂等失败记录与逐单元成功/隔离审计。
具体工作流为每个 projector/source identity 只保留一条累积失败记录，每次尝试写 outcome 并推进 attempt，修复后显式 resolved。
示例：`LoopProjectionFailure(projector_name="loop_facts", unit_id="run-1", ...)`。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class LoopProjectionFailure(Base):
    __tablename__ = "loop_projection_failures"
    __table_args__ = (
        UniqueConstraint("loop_id", "projector_name", "unit_kind", "unit_id", name="uq_loop_projection_failure_unit"),
        CheckConstraint("status IN ('retryable','quarantined','resolved')", name="ck_loop_projection_failure_status"),
        CheckConstraint("attempt_count > 0", name="ck_loop_projection_failure_attempts"),
    )

    failure_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    projector_name: Mapped[str] = mapped_column(String(80), nullable=False)
    unit_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    unit_id: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_class: Mapped[str] = mapped_column(String(120), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    boundary_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    first_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoopProjectionUnitOutcome(Base):
    __tablename__ = "loop_projection_unit_outcomes"
    __table_args__ = (
        UniqueConstraint("loop_id", "projector_name", "unit_kind", "unit_id", "attempt", name="uq_loop_projection_outcome_attempt"),
        CheckConstraint("status IN ('succeeded','retryable','quarantined')", name="ck_loop_projection_outcome_status"),
        CheckConstraint("attempt > 0", name="ck_loop_projection_outcome_attempt"),
    )

    outcome_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    projector_name: Mapped[str] = mapped_column(String(80), nullable=False)
    unit_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    unit_id: Mapped[str] = mapped_column(String(160), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    boundary_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
