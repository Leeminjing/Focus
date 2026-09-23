r"""本文件对外提供 Context expansion 当前状态与不可变 transition ORM 实体。

输入为稳定 expansion/opportunity identity、Loop/round、冻结 work spec、source/evidence frontiers、阶段 identity、编译计划与结果引用；
输出为可恢复的 `LoopContextExpansion` 当前记录与 `LoopContextExpansionTransition` 历史。具体工作流为当前行保存最新 revision
和恢复所需不可变合同，每次合法转换追加同 revision 的历史行，终态保留但不再推进。示例：`row = LoopContextExpansion(...)`。
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


class LoopContextExpansion(Base):
    __tablename__ = "loop_context_expansions"
    __table_args__ = (
        UniqueConstraint("loop_id", "opportunity_id", name="uq_loop_context_expansion_opportunity"),
    )

    expansion_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    opportunity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False, index=True)
    source_context_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"), nullable=True, index=True)
    source_revision_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("desktop_context_revisions.revision_id", ondelete="RESTRICT"), nullable=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="signals_collected", server_default="signals_collected")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(24), nullable=False)
    independence_key: Mapped[str] = mapped_column(String(240), nullable=False)
    semantic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    workspace_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    opportunity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    work_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    manifest_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    source_frontier: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    evidence_frontier: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    stage_identities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    compiled_plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    definition_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    planner_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    projector_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolver_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blocker_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    safe_summary: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    correlation_id: Mapped[str] = mapped_column(String(120), nullable=False)
    causation_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoopContextExpansionTransition(Base):
    __tablename__ = "loop_context_expansion_transitions"
    __table_args__ = (
        UniqueConstraint("expansion_id", "revision", name="uq_loop_context_expansion_transition_revision"),
    )

    transition_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    expansion_id: Mapped[str] = mapped_column(String(64), ForeignKey("loop_context_expansions.expansion_id", ondelete="CASCADE"), nullable=False, index=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str] = mapped_column(String(24), nullable=False)
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    blocker_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    safe_summary: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
