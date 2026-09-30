"""本文件对外提供独立于展示保留窗口的 DesktopDomainResult 持久来源。

输入为领域 kind、稳定 identity、版本 hash、类型化结果与独立审计定位；输出为不可变来源行。
具体工作流为可信提交端口幂等写入，两个消费者按相同 identity 分别维护自身游标或 receipts。
示例：DesktopDomainResult(result_key="hash", kind="test", source_id="test-id", ...)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from focus.persistence.base import Base
from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class DesktopDomainResult(Base):
    __tablename__ = "desktop_domain_results"
    __table_args__ = (
        UniqueConstraint(
            "kind", "source_id", "version", name="uq_domain_result_version"
        ),
    )

    result_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    loop_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    context_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="RESTRICT")
    )
    run_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("desktop_runs.run_id", ondelete="RESTRICT")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    audit: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
