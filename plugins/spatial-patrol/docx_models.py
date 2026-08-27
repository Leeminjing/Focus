"""Plugin-owned persistence for recoverable DOCX editing sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class DocxEditorSession(Base):
    __tablename__ = "docx_editor_sessions"

    session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    content_ref: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="created")
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    opened_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    saved_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    dirty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recovery_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    callback_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "document_id": self.document_id,
            "task_id": self.task_id,
            "content_ref": self.content_ref,
            "mode": self.mode,
            "status": self.status,
            "permissions": self.permissions,
            "opened_hash": self.opened_hash,
            "saved_hash": self.saved_hash,
            "document_version": self.document_version,
            "dirty": self.dirty,
            "callback_status": self.callback_status,
            "last_error": self.last_error,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
