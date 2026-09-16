r"""本文件对外提供 MainRunSettled 持久 outbox 与消费 receipt 的 SQLAlchemy 实体。

输入为 Run 权威终态事件、delivery claim 和 consumer identity；输出为可恢复事件行与唯一消费事实。
具体工作流为 Run finalization 同事务写 event，后台消费者领取后将领域副作用和 receipt 同事务提交，
进程重启继续处理 pending/error 记录。示例：`event = RunOutboxEvent(event_id="...", ...)`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.desktop import models as _desktop_models
from focus.persistence.base import Base


class RunOutboxEvent(Base):
    __tablename__ = "run_outbox_events"
    __table_args__ = (
        UniqueConstraint("run_id", "event_type", name="uq_run_outbox_result"),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'delivered', 'error')",
            name="ck_run_outbox_status",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_runs.run_id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    delivery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    claimed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunOutboxDelivery(Base):
    __tablename__ = "run_outbox_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "consumer_id", name="uq_run_outbox_delivery"),
    )

    delivery_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("run_outbox_events.event_id", ondelete="CASCADE"), nullable=False, index=True
    )
    consumer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
