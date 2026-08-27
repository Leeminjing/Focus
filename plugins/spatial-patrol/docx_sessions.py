"""Workspace-safe, single-writer DOCX session lifecycle."""

from __future__ import annotations

import hashlib
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from plugins.spatial_patrol import docx_db
from plugins.spatial_patrol.docx_models import DocxEditorSession
from plugins.spatial_patrol.docx_storage import file_sha256, validate_docx


ACTIVE_STATUSES = frozenset({"created", "ready", "editing", "dirty", "saving", "recoverable"})
TERMINAL_STATUSES = frozenset({"closed", "expired", "conflict", "error"})


class SessionError(RuntimeError):
    pass


class SessionConflict(SessionError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def resolve_workspace_docx(workspace: str | Path, content_ref: str) -> Path:
    if not content_ref or Path(content_ref).is_absolute():
        raise SessionError("DOCX 路径必须是工作区内相对路径")
    root = Path(workspace).resolve()
    candidate = (root / content_ref).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SessionError("DOCX 路径越出工作区") from exc
    if candidate.suffix.lower() != ".docx":
        raise SessionError("仅支持 .docx 编辑会话")
    if not candidate.is_file():
        raise SessionError("DOCX 文件不存在")
    return candidate


def document_id(task_id: str, content_ref: str) -> str:
    normalized = os.path.normcase(content_ref.replace("\\", "/"))
    return hashlib.sha256(f"{task_id}\0{normalized}".encode()).hexdigest()


class DocxSessionManager:
    def __init__(self, session_factory=None, ttl_seconds: int = 4 * 60 * 60) -> None:
        self._provided_factory = session_factory
        self._ttl_seconds = ttl_seconds

    def _factory(self):
        if self._provided_factory is not None:
            return self._provided_factory
        from focus.persistence.engine import get_session_factory

        return get_session_factory()

    async def _workspace(self, session: Any, task_id: str) -> str:
        from backend.app.desktop.models import DesktopThread, DesktopWorkspace

        task = await session.get(DesktopThread, task_id)
        if not task:
            raise SessionError("任务不存在")
        workspace = await session.get(DesktopWorkspace, task.workspace_id)
        if not workspace:
            raise SessionError("工作区不存在")
        return workspace.path

    async def create(self, *, task_id: str, content_ref: str, mode: str) -> DocxEditorSession:
        if mode not in {"view", "edit"}:
            raise SessionError("会话模式必须是 view 或 edit")
        if self._provided_factory is None:
            await docx_db.ensure_tables()
        factory = self._factory()
        async with factory() as session:
            workspace = await self._workspace(session, task_id)
            path = resolve_workspace_docx(workspace, content_ref)
            await asyncio.to_thread(validate_docx, path)
            now = utcnow()
            doc_id = document_id(task_id, content_ref)
            rows = list((await session.scalars(
                select(DocxEditorSession).where(
                    DocxEditorSession.document_id == doc_id,
                    DocxEditorSession.status.in_(ACTIVE_STATUSES),
                )
            )).all())
            for existing in rows:
                expiry = existing.expires_at
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= now:
                    existing.status = "expired"
                elif mode == "edit" and existing.mode == "edit":
                    raise SessionConflict("该 DOCX 已有活动写会话")
            digest = await asyncio.to_thread(file_sha256, path)
            record = DocxEditorSession(
                session_id=uuid.uuid4().hex,
                document_id=doc_id,
                task_id=task_id,
                content_ref=content_ref.replace("\\", "/"),
                mode=mode,
                status="created",
                permissions={
                    "edit": mode == "edit",
                    "download": True,
                    "print": True,
                    "review": mode == "edit",
                },
                opened_hash=digest,
                saved_hash=digest,
                document_version=1,
                dirty=False,
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def acquire(
        self, *, task_id: str, content_ref: str, mode: str
    ) -> tuple[DocxEditorSession, bool]:
        """Create a session, or resume this task's existing editor session."""
        try:
            return await self.create(
                task_id=task_id, content_ref=content_ref, mode=mode
            ), False
        except SessionConflict:
            if mode != "edit":
                raise
            record = await self.find_active(
                task_id=task_id, content_ref=content_ref, require_edit=True
            )
            return await self.update(record.session_id), True

    async def get(self, session_id: str) -> DocxEditorSession:
        if self._provided_factory is None:
            await docx_db.ensure_tables()
        factory = self._factory()
        async with factory() as session:
            record = await session.get(DocxEditorSession, session_id)
            if not record:
                raise SessionError("DOCX 会话不存在")
            expiry = record.expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= utcnow() and record.status in ACTIVE_STATUSES:
                record.status = "expired"
                await session.commit()
                raise SessionError("DOCX 会话已过期")
            return record

    async def resolve_path(self, record: DocxEditorSession) -> Path:
        factory = self._factory()
        async with factory() as session:
            workspace = await self._workspace(session, record.task_id)
        return resolve_workspace_docx(workspace, record.content_ref)

    async def update(self, session_id: str, **changes: Any) -> DocxEditorSession:
        allowed = {
            "status", "dirty", "recovery_url", "callback_status", "last_error",
            "saved_hash", "opened_hash", "document_version", "expires_at",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise SessionError(f"不可更新会话字段: {', '.join(sorted(unknown))}")
        factory = self._factory()
        async with factory() as session:
            record = await session.get(DocxEditorSession, session_id)
            if not record:
                raise SessionError("DOCX 会话不存在")
            for name, value in changes.items():
                setattr(record, name, value)
            record.expires_at = utcnow() + timedelta(seconds=self._ttl_seconds)
            await session.commit()
            await session.refresh(record)
            return record

    async def record_edit_result(
        self, session_id: str, *, changed: bool, error: str | None = None
    ) -> DocxEditorSession:
        record = await self.get(session_id)
        changes: dict[str, Any] = {"last_error": error}
        if changed:
            changes.update(
                dirty=True,
                status="dirty",
                document_version=record.document_version + 1,
            )
        elif error:
            changes["status"] = "recoverable"
        return await self.update(session_id, **changes)

    async def close(self, session_id: str, *, discard_unsaved: bool = False) -> DocxEditorSession:
        record = await self.get(session_id)
        if record.dirty and not discard_unsaved:
            raise SessionConflict("DOCX 有未保存更改")
        return await self.update(
            session_id, status="closed", dirty=False,
            recovery_url=None if discard_unsaved else record.recovery_url,
        )

    async def has_active_writer(self, *, task_id: str, content_ref: str) -> bool:
        doc_id = document_id(task_id, content_ref)
        factory = self._factory()
        async with factory() as session:
            record = await session.scalar(
                select(DocxEditorSession.session_id).where(
                    DocxEditorSession.document_id == doc_id,
                    DocxEditorSession.mode == "edit",
                    DocxEditorSession.status.in_(ACTIVE_STATUSES),
                    DocxEditorSession.expires_at > utcnow(),
                ).limit(1)
            )
        return record is not None

    async def find_active(
        self, *, task_id: str, content_ref: str, require_edit: bool = False
    ) -> DocxEditorSession:
        if self._provided_factory is None:
            await docx_db.ensure_tables()
        doc_id = document_id(task_id, content_ref)
        factory = self._factory()
        conditions = [
            DocxEditorSession.document_id == doc_id,
            DocxEditorSession.status.in_(ACTIVE_STATUSES),
            DocxEditorSession.expires_at > utcnow(),
        ]
        if require_edit:
            conditions.append(DocxEditorSession.mode == "edit")
        async with factory() as session:
            record = await session.scalar(
                select(DocxEditorSession).where(*conditions).order_by(
                    DocxEditorSession.updated_at.desc()
                ).limit(1)
            )
        if not record:
            requirement = "活动编辑会话" if require_edit else "活动会话"
            raise SessionError(f"请先在 Word 式编辑器中打开该 DOCX 的{requirement}")
        return record


session_manager = DocxSessionManager()
