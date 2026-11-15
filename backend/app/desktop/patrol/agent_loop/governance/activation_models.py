r"""本文件对外提供已退役 Context Loop 的历史激活账务读模型。

输入为已有激活身份；输出为初始 Run 与历史授权的关联。
具体工作流为仅供历史迁移注册及 LoopAccountingQuery 读取，避免退役后丢失已有消费的归属。
旧启动接口和写入用例已移除，Workspace Patrol 不创建激活记录。
示例：`select(LoopActivation).where(LoopActivation.loop_id == loop_id)`。
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
