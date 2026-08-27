"""Idempotent plugin-local DDL; deliberately outside Focus Alembic."""

from __future__ import annotations

import asyncio


_ready = False
_lock = asyncio.Lock()


async def ensure_tables() -> None:
    global _ready
    if _ready:
        return
    async with _lock:
        if _ready:
            return
        from focus.persistence.engine import get_session_factory
        # Register the FK target in shared Base.metadata before compiling this table.
        from backend.app.desktop import models as _desktop_models  # noqa: F401
        from plugins.spatial_patrol.docx_models import DocxEditorSession

        factory = get_session_factory()
        async with factory() as session:
            await session.run_sync(
                lambda sync_session: DocxEditorSession.__table__.create(
                    sync_session.connection(), checkfirst=True
                )
            )
            await session.commit()
        _ready = True
