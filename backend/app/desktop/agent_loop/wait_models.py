r"""本文件对外提供 LoopWaitRequest 与 LoopWaitResponse 持久化实体。

输入为 Loop 等待原因、类型化响应合同、因果标识、乐观版本与用户响应；输出为可审计且至多一个开放请求、
至多一个已提交响应的数据库状态。具体工作流为请求随 Loop 进入 waiting_user 原子创建，响应以 idempotency key
去重并引用请求 revision，随后请求转入 resolved、cancelled 或 superseded。示例：`LoopWaitRequest(kind="clarification", ...)`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class LoopWaitRequest(Base):
    __tablename__ = "loop_wait_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open','resolving','resolved','cancelled','superseded')",
            name="ck_loop_wait_request_status",
        ),
        CheckConstraint(
            "response_mode IN ('text','single_choice','multiple_choice','structured','action')",
            name="ck_loop_wait_request_response_mode",
        ),
        CheckConstraint("revision > 0", name="ck_loop_wait_request_revision"),
        Index(
            "uq_loop_wait_request_active",
            "loop_id",
            unique=True,
            postgresql_where=text("status IN ('open','resolving')"),
        ),
    )

    request_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("loop_rounds.round_id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    response_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    response_contract: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", server_default="open")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    correlation_id: Mapped[str] = mapped_column(String(120), nullable=False)
    causation_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoopWaitResponse(Base):
    __tablename__ = "loop_wait_responses"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_loop_wait_response_request"),
        UniqueConstraint("idempotency_key", name="uq_loop_wait_response_idempotency"),
        CheckConstraint("request_revision > 0", name="ck_loop_wait_response_revision"),
    )

    response_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("loop_wait_requests.request_id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    answer: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
