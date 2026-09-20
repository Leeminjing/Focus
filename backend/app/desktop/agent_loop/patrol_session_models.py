r"""本文件对外提供 Patrol Session 与 phase transition ORM 实体。

输入为 Loop/round identity、当前 fencing token、冻结 observation 引用、安全活动摘要、等待目标和 Curator scope；输出为
可恢复的 `LoopPatrolSession`、不可变 `LoopPatrolPhaseTransition` 历史与 `LoopCuratorAssignment` 当前记录。具体工作流为
Session 保存当前投影，Transition/Assignment 按 revision 追加因果历史，进程重启后从已提交状态继续或收敛。
示例：`session = LoopPatrolSession(...)`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class LoopPatrolSession(Base):
    __tablename__ = "loop_patrol_sessions"
    __table_args__ = (UniqueConstraint("round_id", name="uq_loop_patrol_session_round"),)

    session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    current_phase: Mapped[str] = mapped_column(String(32), nullable=False, default="created", server_default="created")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    observation_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    safe_summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    wait_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    wait_targets: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    terminal_outcome: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoopPatrolPhaseTransition(Base):
    __tablename__ = "loop_patrol_phase_transitions"
    __table_args__ = (UniqueConstraint("session_id", "revision", name="uq_loop_patrol_phase_revision"),)

    transition_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_patrol_sessions.session_id", ondelete="CASCADE"), nullable=False, index=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    from_phase: Mapped[str] = mapped_column(String(32), nullable=False)
    to_phase: Mapped[str] = mapped_column(String(32), nullable=False)
    safe_summary: Mapped[str] = mapped_column(Text, nullable=False)
    wait_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    wait_targets: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoopCuratorAssignment(Base):
    __tablename__ = "loop_curator_assignments"
    __table_args__ = (
        UniqueConstraint("session_id", "assignment_key", name="uq_loop_curator_assignment_key"),
        UniqueConstraint("worker_request_id", name="uq_loop_curator_worker_request"),
    )

    assignment_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    assignment_key: Mapped[str] = mapped_column(String(160), nullable=False)
    session_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_patrol_sessions.session_id", ondelete="CASCADE"), nullable=False, index=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False, index=True)
    worker_request_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_worker_requests.worker_request_id", ondelete="CASCADE"), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
