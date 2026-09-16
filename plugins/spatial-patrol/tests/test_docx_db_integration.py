"""本文件验证 spatial-patrol DOCX 会话表与单写者生命周期。

输入为测试套件提供的隔离 PostgreSQL 和临时 DOCX 工作区；输出为幂等建表、编辑会话复用、
读写权限、过期恢复及脏会话关闭断言。具体工作流为每个同步测试在独立事件循环中初始化
Focus 数据库引擎，执行插件会话操作，并在离开事件循环前通过公开释放端口清空进程级引擎。
例如，第二个写会话会被拒绝，而过期写会话允许由新的会话接管。
"""

import asyncio
import uuid
import zipfile
from datetime import timedelta

from sqlalchemy import text


def test_docx_session_table_is_created_idempotently_in_plugin_database():
    async def scenario():
        from focus.config import get_app_config
        from focus.persistence.engine import dispose_engine, get_session_factory, init_engine
        from plugins.spatial_patrol import docx_db

        init_engine(get_app_config("config.yaml"))
        try:
            docx_db._ready = False
            await docx_db.ensure_tables()
            await docx_db.ensure_tables()
            async with get_session_factory()() as session:
                name = await session.scalar(text("select to_regclass('public.docx_editor_sessions')"))
            assert name == "docx_editor_sessions"
        finally:
            await dispose_engine()

    asyncio.run(scenario())


def test_session_manager_enforces_single_writer_expiry_and_recovery(tmp_path):
    async def scenario():
        from sqlalchemy import delete
        from backend.app.desktop.models import DesktopThread, DesktopWorkspace
        from focus.config import get_app_config
        from focus.persistence.engine import dispose_engine, get_session_factory, init_engine
        from plugins.spatial_patrol import docx_db
        from plugins.spatial_patrol.docx_models import DocxEditorSession
        from plugins.spatial_patrol.docx_sessions import (
            DocxSessionManager, SessionConflict, SessionError, utcnow,
        )

        workspace_id = uuid.uuid4().hex
        task_id = uuid.uuid4().hex
        workspace = tmp_path / workspace_id
        workspace.mkdir()
        with zipfile.ZipFile(workspace / "complex.docx", "w") as package:
            package.writestr("[Content_Types].xml", "<Types/>")
            package.writestr("_rels/.rels", "<Relationships/>")
            package.writestr("word/document.xml", "<document/>")
        init_engine(get_app_config("config.yaml"))
        docx_db._ready = False
        await docx_db.ensure_tables()
        factory = get_session_factory()
        async with factory() as session:
            session.add(DesktopWorkspace(
                workspace_id=workspace_id, path=str(workspace), display_name="docx-test"
            ))
            await session.flush()
            session.add(DesktopThread(
                task_id=task_id, workspace_id=workspace_id,
                thread_id=f"docx-{task_id}", title="docx-test",
            ))
            await session.commit()
        manager = DocxSessionManager(ttl_seconds=60)
        try:
            writer = await manager.create(
                task_id=task_id, content_ref="complex.docx", mode="edit"
            )
            assert writer.permissions["edit"] is True
            resumed, reused = await manager.acquire(
                task_id=task_id, content_ref="complex.docx", mode="edit"
            )
            assert reused is True
            assert resumed.session_id == writer.session_id
            try:
                await manager.create(
                    task_id=task_id, content_ref="complex.docx", mode="edit"
                )
                raise AssertionError("second writer should fail")
            except SessionConflict:
                pass
            reader = await manager.create(
                task_id=task_id, content_ref="complex.docx", mode="view"
            )
            assert reader.permissions["edit"] is False
            async with factory() as session:
                row = await session.get(DocxEditorSession, writer.session_id)
                row.expires_at = utcnow() - timedelta(seconds=1)
                await session.commit()
            try:
                await manager.get(writer.session_id)
                raise AssertionError("expired session should fail")
            except SessionError:
                pass
            replacement = await manager.create(
                task_id=task_id, content_ref="complex.docx", mode="edit"
            )
            assert replacement.session_id != writer.session_id
            await manager.update(replacement.session_id, dirty=True, status="dirty")
            try:
                await manager.close(replacement.session_id)
                raise AssertionError("dirty close should require confirmation")
            except SessionConflict:
                pass
            closed = await manager.close(replacement.session_id, discard_unsaved=True)
            assert closed.status == "closed" and closed.dirty is False
        finally:
            async with factory() as session:
                await session.execute(delete(DesktopWorkspace).where(
                    DesktopWorkspace.workspace_id == workspace_id
                ))
                await session.commit()
            await dispose_engine()

    asyncio.run(scenario())
