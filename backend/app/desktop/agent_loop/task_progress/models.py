"""本文件对外提供任务记忆版本、冻结输入、后台工作与来源吸收的 ORM 模型。

输入为已校验记忆合同和唯一 Round/来源身份；输出为不可变业务内容与独立可变工作状态。
具体工作流为一次冻结登记 work，领取以 fence 排他，发布同时写版本、贡献和 receipts。
示例：LoopProgressReceipt(loop_id="l", source_key="hash", progress_id="p")。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from focus.persistence.base import Base
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class LoopTaskProgress(Base):
    __tablename__ = "loop_task_progress"
    __table_args__ = (
        UniqueConstraint("loop_id", "generation", name="uq_task_progress_generation"),
        UniqueConstraint("observation_id", name="uq_task_progress_observation"),
    )

    progress_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("loop_observations.observation_id", ondelete="RESTRICT")
    )
    previous_progress_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("loop_task_progress.progress_id", ondelete="RESTRICT")
    )
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    contribution: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LoopProgressHead(Base):
    __tablename__ = "loop_progress_heads"

    loop_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("agent_loops.loop_id", ondelete="CASCADE"),
        primary_key=True,
    )
    progress_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("loop_task_progress.progress_id", ondelete="RESTRICT"),
        nullable=False,
    )


class LoopDecisionInputs(Base):
    __tablename__ = "loop_decision_inputs"

    observation_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("loop_observations.observation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    loop_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), index=True
    )
    round_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), unique=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class LoopProgressWork(Base):
    __tablename__ = "loop_progress_work"

    observation_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("loop_decision_inputs.observation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    loop_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), index=True
    )
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    fence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    retry_budget_authorization: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempt_events: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LoopDecisionSupplement(Base):
    __tablename__ = "loop_decision_supplements"
    __table_args__ = (
        UniqueConstraint("observation_id", "kind", name="uq_decision_supplement_kind"),
    )

    supplement_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    observation_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("loop_observations.observation_id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class LoopProgressReceipt(Base):
    __tablename__ = "loop_progress_receipts"

    loop_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("agent_loops.loop_id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    progress_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("loop_task_progress.progress_id", ondelete="RESTRICT"),
        nullable=False,
    )
