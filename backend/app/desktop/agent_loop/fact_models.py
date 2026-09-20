r"""本文件对外提供 LoopFact、LoopFactRevision 与 LoopFactRelationship 持久化实体。

输入为稳定事实 identity、规范化 subject、来源 Context/Run、类型化证据、验证主体及事实关系；输出为当前事实指针、
不可变修订历史与不删除旧结论的 supersedes/contradicts 关系。具体工作流为 current row 保存最新 revision 和可查询摘要，
revision row 保存每次状态变化的完整快照，relationship row 连接跨观察结论。示例：`session.add(LoopFact(...))`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class LoopFact(Base):
    __tablename__ = "loop_facts"
    __table_args__ = (
        CheckConstraint("state IN ('observed','verifying','verified','contradicted','superseded')", name="ck_loop_fact_state"),
        UniqueConstraint("loop_id", "identity_key", name="uq_loop_fact_identity"),
        Index("ix_loop_fact_current_subject", "loop_id", "fact_type", "normalized_subject", "updated_at"),
    )

    fact_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    identity_key: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    normalized_subject: Mapped[str] = mapped_column(String(500), nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="observed", server_default="observed")
    source_context_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("desktop_threads.task_id", ondelete="SET NULL"), nullable=True, index=True)
    source_run_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("desktop_runs.run_id", ondelete="SET NULL"), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    presentation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    observer: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    verifier: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LoopFactRevision(Base):
    __tablename__ = "loop_fact_revisions"
    __table_args__ = (
        UniqueConstraint("fact_id", "revision", name="uq_loop_fact_revision"),
        CheckConstraint("state IN ('observed','verifying','verified','contradicted','superseded')", name="ck_loop_fact_revision_state"),
    )

    fact_revision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    fact_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_facts.fact_id", ondelete="CASCADE"), nullable=False, index=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    cause_event_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    presentation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    observer: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    verifier: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoopFactRelationship(Base):
    __tablename__ = "loop_fact_relationships"
    __table_args__ = (
        UniqueConstraint("source_fact_id", "target_fact_id", "relation", name="uq_loop_fact_relationship"),
        CheckConstraint("relation IN ('supersedes','contradicts')", name="ck_loop_fact_relationship_kind"),
    )

    relationship_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    source_fact_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_facts.fact_id", ondelete="CASCADE"), nullable=False, index=True)
    target_fact_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_facts.fact_id", ondelete="CASCADE"), nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(24), nullable=False)
    cause_event_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
