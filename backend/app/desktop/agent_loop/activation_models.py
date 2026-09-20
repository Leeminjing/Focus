r"""本文件对外提供 LoopActivation 不可变授权 lineage 实体。

输入为新 Loop、选中的直接用户 Run、可选前置 Loop、readiness token 与幂等 activation key；输出为独立于历史 AgentLoop 行结构的授权事实。
具体工作流为创建事务锁定 eligibility 后写唯一 activation，等价重试复用该行，前置 Loop 与 Run 标识永不改写。
示例：`LoopActivation(loop_id="l2", selected_run_id="r2", predecessor_loop_id="l1", ...)`。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class LoopActivation(Base):
    __tablename__ = "loop_activations"
    __table_args__ = (
        UniqueConstraint("loop_id", name="uq_loop_activation_loop"),
        UniqueConstraint("activation_key", name="uq_loop_activation_key"),
        UniqueConstraint("selected_run_id", name="uq_loop_activation_selected_run"),
    )

    activation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    selected_run_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_runs.run_id", ondelete="RESTRICT"), nullable=False, index=True)
    predecessor_loop_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="SET NULL"), nullable=True, index=True)
    readiness_token: Mapped[str] = mapped_column(String(64), nullable=False)
    activation_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
