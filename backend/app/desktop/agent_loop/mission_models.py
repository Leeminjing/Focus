r"""本文件对外提供 LoopMissionRevision 持久化实体。

输入为用户确认的最终结果、结构化执行边界、完成检查定义和修订作者；输出为按 Loop 与 revision 唯一、
可追溯到旧 Goal revision 的不可变 Mission revision 记录。具体工作流为领域服务先验证 Mission contract，
Repository 再追加新 revision，现有 AgentLoop.goal_revision 在迁移期继续作为当前 Mission revision 游标。
示例：`LoopMissionRevision(loop_id="loop-1", revision=1, outcome="完成发布", ...)`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class LoopMissionRevision(Base):
    __tablename__ = "loop_mission_revisions"
    __table_args__ = (
        UniqueConstraint("loop_id", "revision", name="uq_loop_mission_revision"),
        CheckConstraint("revision > 0", name="ck_loop_mission_revision_positive"),
    )

    mission_revision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    boundaries: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    completion_checks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    legacy_goal_revision_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("loop_goal_revisions.goal_revision_id", ondelete="SET NULL"), nullable=True
    )
    authored_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
