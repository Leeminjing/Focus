r"""本文件对外提供不可变 Context revision 与版本化来源边的 SQLAlchemy 实体。

输入为 Context identity、执行 checkpoint、投影、哈希、来源和生命周期事实；输出为独立 ORM 表定义。
具体工作流为复用 schema 层枚举合同，持久化 revision 内容与有序来源，并由
`DesktopThread.current_revision_id` 指向已发布版本。
示例：`revision = ContextRevision(revision_id="r1", context_id="c1", generation=1, ...)`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base

from backend.app.desktop.context_evolution.schemas import (
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
)


class ContextRevision(Base):
    __tablename__ = "desktop_context_revisions"
    __table_args__ = (
        UniqueConstraint("context_id", "generation", name="uq_context_revision_generation"),
        UniqueConstraint(
            "execution_thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_context_revision_execution_checkpoint",
        ),
        CheckConstraint("generation > 0", name="ck_context_revision_generation_positive"),
        CheckConstraint(
            "payload_mode IN ('checkpoint', 'definition')",
            name="ck_context_revision_payload_mode",
        ),
        CheckConstraint(
            "projection_status IN ('preparing', 'valid', 'repaired', 'approved', 'approval_required', "
            "'rejected', 'error', 'deleted')",
            name="ck_context_revision_projection_status",
        ),
        CheckConstraint(
            "origin_kind IN ('root', 'manual_derive', 'definition_update', 'projection_decision', "
            "'compression', 'compression_restore', 'run_settled', 'curation', 'migration')",
            name="ck_context_revision_origin_kind",
        ),
        CheckConstraint(
            "payload_mode <> 'checkpoint' OR checkpoint_id IS NOT NULL",
            name="ck_context_revision_checkpoint_payload",
        ),
    )

    revision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    context_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False, index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_mode: Mapped[str] = mapped_column(String(16), nullable=False)
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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContextRevisionSource(Base):
    __tablename__ = "desktop_context_revision_sources"
    __table_args__ = (
        UniqueConstraint("target_revision_id", "position", name="uq_context_revision_source_position"),
        UniqueConstraint(
            "target_revision_id",
            "source_revision_id",
            name="uq_context_revision_source_revision",
        ),
        CheckConstraint("position >= 0", name="ck_context_revision_source_position"),
        CheckConstraint(
            "target_revision_id <> source_revision_id",
            name="ck_context_revision_source_not_self",
        ),
    )

    source_edge_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    target_revision_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_context_revisions.revision_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_context_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_revision_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_context_revisions.revision_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_checkpoint_id: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
