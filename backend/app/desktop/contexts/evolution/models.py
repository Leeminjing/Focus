"""本文件对外提供唯一规范 Context 输入和 owner 纯引用 ORM。

输入为内容、执行身份、直接来源和输入 owner；输出为 desktop_context_inputs 与 desktop_context_input_owners 表定义。
具体工作流为正文按 token 只存一次，current pointer 和 owner 引用共同保护输入；无根输入及时回收而不积累版本档案。
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
    __tablename__ = "desktop_context_inputs"
    __table_args__ = (
        UniqueConstraint(
            "execution_thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_context_input_execution_checkpoint",
        ),
        CheckConstraint("generation > 0", name="ck_context_input_generation_positive"),
        CheckConstraint(
            "payload_mode IN ('checkpoint', 'definition')",
            name="ck_context_input_payload_mode",
        ),
        CheckConstraint(
            "projection_status IN ('preparing', 'valid', 'repaired', 'approved', 'approval_required', "
            "'rejected', 'error', 'deleted')",
            name="ck_context_input_projection_status",
        ),
        CheckConstraint(
            "origin_kind IN ('root', 'manual_derive', 'definition_update', 'projection_decision', "
            "'compression', 'compression_restore', 'run_settled', 'curation', 'migration')",
            name="ck_context_input_origin_kind",
        ),
        CheckConstraint(
            "payload_mode <> 'checkpoint' OR checkpoint_id IS NOT NULL",
            name="ck_context_input_checkpoint_payload",
        ),
    )

    revision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    context_id: Mapped[str] = mapped_column(
        String(32),
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
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
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
    __tablename__ = "desktop_context_input_owners"
    __table_args__ = (
        UniqueConstraint(
            "owner_kind",
            "owner_id",
            "revision_id",
            name="uq_context_input_owner",
        ),
    )

    snapshot_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    revision_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_context_inputs.revision_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
