"""本文件对外提供唯一当前 Context 状态及 owner 持有的临时输入快照 ORM。

输入为内容、执行身份、当前跨 Context 来源和快照 owner；输出为 desktop_context_states 与 desktop_context_input_snapshots 表定义。
具体工作流为 context_id 唯一约束限制当前状态，快照按 owner 与精确 token 隔离，并保存真实发布资格供认知读取校验。
示例：session.get(ContextRevision, current_token)；ContextInputSnapshot(owner_kind="run", owner_id=run_id, ...)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from focus.persistence.base import Base
from sqlalchemy import (
    Boolean,
    CheckConstraint,
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


class ContextRevision(Base):
    __tablename__ = "desktop_context_states"
    __table_args__ = (
        UniqueConstraint("context_id", name="uq_context_state_context"),
        UniqueConstraint(
            "execution_thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_context_state_execution_checkpoint",
        ),
        CheckConstraint("generation > 0", name="ck_context_state_generation_positive"),
        CheckConstraint(
            "payload_mode IN ('checkpoint', 'definition')",
            name="ck_context_state_payload_mode",
        ),
        CheckConstraint(
            "projection_status IN ('preparing', 'valid', 'repaired', 'approved', 'approval_required', "
            "'rejected', 'error', 'deleted')",
            name="ck_context_state_projection_status",
        ),
        CheckConstraint(
            "origin_kind IN ('root', 'manual_derive', 'definition_update', 'projection_decision', "
            "'compression', 'compression_restore', 'run_settled', 'curation', 'migration')",
            name="ck_context_state_origin_kind",
        ),
        CheckConstraint(
            "payload_mode <> 'checkpoint' OR checkpoint_id IS NOT NULL",
            name="ck_context_state_checkpoint_payload",
        ),
    )

    revision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    context_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sources: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    history_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    authored_messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    execution_messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    repair_manifest: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    initial_message_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    definition_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    projection_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_status: Mapped[str] = mapped_column(String(32), nullable=False)
    origin_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    origin_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ContextInputSnapshot(Base):
    __tablename__ = "desktop_context_input_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "owner_kind",
            "owner_id",
            "revision_id",
            name="uq_context_input_snapshot_owner",
        ),
    )

    snapshot_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    revision_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    context_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
