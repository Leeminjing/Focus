"""会话归档/删除生命周期测试：归档可恢复、删除后代无关、有后代墓碑、级联、409 保护、lifecycle。

测试复用项目既有模式（真实 Postgres engine + 独立 loop，不触碰全局单例）。
"""

import asyncio
import atexit
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# asyncpg 连接池绑定事件循环：模块级共享一个 loop，不得 set_event_loop（避免污染 TestClient）
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
_LOOP = asyncio.new_event_loop()

# 独立 engine，不碰全局单例
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

_ENGINE = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
_SESSION_FACTORY = async_sessionmaker(_ENGINE, expire_on_commit=False)
atexit.register(lambda: _LOOP.run_until_complete(_ENGINE.dispose()))

from backend.app.desktop.context_service import ContextService  # noqa: E402
from backend.app.desktop.models import (  # noqa: E402
    DesktopContextSource,
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
)
from focus.config.app_config import AppConfig  # noqa: E402


class FakeCheckpointer:
    """记录 adelete_thread 调用；aget_tuple 恒返回 None（archive/delete 路径不使用）。"""

    def __init__(self) -> None:
        self.deleted_threads: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted_threads.append(thread_id)

    async def aget_tuple(self, config) -> None:
        return None


def _app_config() -> AppConfig:
    return AppConfig.model_validate({
        "models": [{
            "name": "deepseek-v4-flash", "display_name": "m", "use": "x:y",
            "model": "deepseek-v4-flash", "api_key": "k", "base_url": "http://x",
            "context_window": 131072,
        }]
    })


def _contexts(checkpointer=None) -> ContextService:
    return ContextService(_SESSION_FACTORY, checkpointer or FakeCheckpointer(), _app_config())


async def _seed_workspace(session) -> str:
    workspace_id = f"ws-{uuid.uuid4().hex[:14]}"
    session.add(DesktopWorkspace(workspace_id=workspace_id, path=f"/tmp/{workspace_id}", display_name="t"))
    await session.flush()
    return workspace_id


async def _seed_thread(session, workspace_id: str, title: str) -> str:
    task_id = f"ct-{uuid.uuid4().hex[:14]}"
    session.add(DesktopThread(
        task_id=task_id, workspace_id=workspace_id,
        thread_id=f"th-{uuid.uuid4().hex[:10]}", title=title,
    ))
    await session.flush()
    return task_id


async def _link_child(session, child_task_id: str, parent_task_id: str) -> None:
    session.add(DesktopContextSource(
        source_id=f"src-{uuid.uuid4().hex[:10]}", context_id=child_task_id,
        parent_context_id=parent_task_id, source_checkpoint_id="ckp-1", position=0,
    ))
    await session.flush()


async def _seed_main_run(session, task_id: str, status: str = "pending") -> None:
    session.add(DesktopRun(
        run_id=f"run-{uuid.uuid4().hex[:10]}", task_id=task_id,
        agent_id=f"main:{task_id}", kind="main", status=status, input_messages=[],
    ))
    await session.flush()


async def _cleanup(workspace_id: str, task_ids: list[str]) -> None:
    async with _SESSION_FACTORY() as session:
        # 先删后代 source 行，避免 parent RESTRICT 拦截
        await session.execute(
            delete(DesktopContextSource).where(DesktopContextSource.parent_context_id.in_(task_ids))
        )
        for tid in task_ids:
            await session.execute(delete(DesktopThread).where(DesktopThread.task_id == tid))
        await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id))
        await session.commit()


def test_archive_no_descendant_marks_and_lists_archived():
    async def run() -> None:
        contexts = _contexts()
        async with _SESSION_FACTORY() as session:
            ws = await _seed_workspace(session)
            tid = await _seed_thread(session, ws, "root")
            await session.commit()
        try:
            result = await contexts.archive(tid)
            assert result == {"context_id": tid, "archived": True}
            async with _SESSION_FACTORY() as session:
                task = await session.get(DesktopThread, tid)
                assert task.archived_at is not None
            payload = await contexts.get(tid)
            assert payload["lifecycle"] == "archived"
            assert any(item["context_id"] == tid for item in await contexts.list_archived())
        finally:
            await _cleanup(ws, [tid])
    _LOOP.run_until_complete(run())


def test_archive_cascade_recursively_archives_lineage():
    async def run() -> None:
        contexts = _contexts()
        async with _SESSION_FACTORY() as session:
            ws = await _seed_workspace(session)
            parent = await _seed_thread(session, ws, "parent")
            child = await _seed_thread(session, ws, "child")
            await _link_child(session, child, parent)
            await session.commit()
        try:
            await contexts.archive(parent, cascade=True)
            async with _SESSION_FACTORY() as session:
                assert (await session.get(DesktopThread, parent)).deleted_at is None
                assert (await session.get(DesktopThread, parent)).archived_at is not None
                assert (await session.get(DesktopThread, child)).archived_at is not None
        finally:
            await _cleanup(ws, [parent, child])
    _LOOP.run_until_complete(run())


def test_unarchive_restores_to_active():
    async def run() -> None:
        contexts = _contexts()
        async with _SESSION_FACTORY() as session:
            ws = await _seed_workspace(session)
            tid = await _seed_thread(session, ws, "root")
            await session.commit()
        try:
            await contexts.archive(tid)
            assert any(item["context_id"] == tid for item in await contexts.list_archived())
            result = await contexts.unarchive(tid)
            assert result == {"context_id": tid, "archived": False}
            async with _SESSION_FACTORY() as session:
                assert (await session.get(DesktopThread, tid)).archived_at is None
            assert not any(item["context_id"] == tid for item in await contexts.list_archived())
        finally:
            await _cleanup(ws, [tid])
    _LOOP.run_until_complete(run())


def test_delete_no_descendant_hard_deletes_and_clears_checkpoint():
    async def run() -> None:
        checkpointer = FakeCheckpointer()
        contexts = _contexts(checkpointer)
        async with _SESSION_FACTORY() as session:
            ws = await _seed_workspace(session)
            tid = await _seed_thread(session, ws, "leaf")
            thread_id = (await session.get(DesktopThread, tid)).thread_id
            await session.commit()
        try:
            result = await contexts.delete(tid)
            assert result == {"context_id": tid, "deleted": True}
            async with _SESSION_FACTORY() as session:
                assert await session.get(DesktopThread, tid) is None
            assert checkpointer.deleted_threads == [thread_id]
        finally:
            await _cleanup(ws, [tid])
    _LOOP.run_until_complete(run())


def test_delete_with_descendant_keeps_tombstone_and_preserves_child():
    async def run() -> None:
        checkpointer = FakeCheckpointer()
        contexts = _contexts(checkpointer)
        async with _SESSION_FACTORY() as session:
            ws = await _seed_workspace(session)
            parent = await _seed_thread(session, ws, "parent")
            child = await _seed_thread(session, ws, "child")
            await _link_child(session, child, parent)
            parent_thread_id = (await session.get(DesktopThread, parent)).thread_id
            await session.commit()
        try:
            await contexts.delete(parent)
            async with _SESSION_FACTORY() as session:
                parent_row = await session.get(DesktopThread, parent)
                child_row = await session.get(DesktopThread, child)
                assert parent_row is not None and parent_row.deleted_at is not None
                assert child_row is not None and child_row.deleted_at is None and child_row.archived_at is None
                # 后代 source 行保留（血缘不断）
                source = await session.scalar(
                    select(DesktopContextSource).where(DesktopContextSource.parent_context_id == parent)
                )
                assert source is not None and source.context_id == child
            assert checkpointer.deleted_threads == [parent_thread_id]
        finally:
            await _cleanup(ws, [parent, child])
    _LOOP.run_until_complete(run())


def test_delete_cascade_removes_whole_lineage_without_tombstone():
    async def run() -> None:
        checkpointer = FakeCheckpointer()
        contexts = _contexts(checkpointer)
        async with _SESSION_FACTORY() as session:
            ws = await _seed_workspace(session)
            parent = await _seed_thread(session, ws, "parent")
            child = await _seed_thread(session, ws, "child")
            await _link_child(session, child, parent)
            parent_thread_id = (await session.get(DesktopThread, parent)).thread_id
            child_thread_id = (await session.get(DesktopThread, child)).thread_id
            await session.commit()
        try:
            await contexts.delete(parent, cascade=True)
            async with _SESSION_FACTORY() as session:
                assert await session.get(DesktopThread, parent) is None
                assert await session.get(DesktopThread, child) is None
            assert set(checkpointer.deleted_threads) == {parent_thread_id, child_thread_id}
        finally:
            await _cleanup(ws, [parent, child])
    _LOOP.run_until_complete(run())


def test_archive_and_delete_block_on_active_run():
    async def run() -> None:
        contexts = _contexts()
        async with _SESSION_FACTORY() as session:
            ws = await _seed_workspace(session)
            tid = await _seed_thread(session, ws, "running")
            await _seed_main_run(session, tid, "running")
            await session.commit()
        try:
            try:
                await contexts.archive(tid)
                raise AssertionError("归档应对运行中会话抛 409")
            except HTTPException as exc:
                assert exc.status_code == 409
            try:
                await contexts.delete(tid)
                raise AssertionError("删除应对运行中会话抛 409")
            except HTTPException as exc:
                assert exc.status_code == 409
        finally:
            await _cleanup(ws, [tid])
    _LOOP.run_until_complete(run())


def test_lifecycle_static_resolves_order():
    task = DesktopThread(task_id="x", workspace_id="w", thread_id="th", title="t")
    assert ContextService._lifecycle(task) == "active"
    task.archived_at = datetime.now(timezone.utc)
    assert ContextService._lifecycle(task) == "archived"
    task.deleted_at = datetime.now(timezone.utc)
    assert ContextService._lifecycle(task) == "deleted"


def test_batch_delete_multiple_no_descendant_hard_deletes():
    async def run() -> None:
        checkpointer = FakeCheckpointer()
        contexts = _contexts(checkpointer)
        async with _SESSION_FACTORY() as session:
            ws = await _seed_workspace(session)
            a = await _seed_thread(session, ws, "a")
            b = await _seed_thread(session, ws, "b")
            thread_a = (await session.get(DesktopThread, a)).thread_id
            thread_b = (await session.get(DesktopThread, b)).thread_id
            await session.commit()
        try:
            result = await contexts.delete_many([a, b])
            assert result == {"context_ids": [a, b], "deleted": True}
            async with _SESSION_FACTORY() as session:
                assert await session.get(DesktopThread, a) is None
                assert await session.get(DesktopThread, b) is None
            assert set(checkpointer.deleted_threads) == {thread_a, thread_b}
        finally:
            await _cleanup(ws, [a, b])
    _LOOP.run_until_complete(run())


def test_batch_delete_with_descendant_keeps_tombstone_and_preserves_child():
    async def run() -> None:
        checkpointer = FakeCheckpointer()
        contexts = _contexts(checkpointer)
        async with _SESSION_FACTORY() as session:
            ws = await _seed_workspace(session)
            parent = await _seed_thread(session, ws, "parent")
            child = await _seed_thread(session, ws, "child")
            leaf = await _seed_thread(session, ws, "leaf")
            await _link_child(session, child, parent)
            parent_thread_id = (await session.get(DesktopThread, parent)).thread_id
            leaf_thread_id = (await session.get(DesktopThread, leaf)).thread_id
            await session.commit()
        try:
            await contexts.delete_many([parent, leaf])
            async with _SESSION_FACTORY() as session:
                parent_row = await session.get(DesktopThread, parent)
                child_row = await session.get(DesktopThread, child)
                leaf_row = await session.get(DesktopThread, leaf)
                assert parent_row is not None and parent_row.deleted_at is not None
                assert child_row is not None and child_row.deleted_at is None
                assert leaf_row is None
                source = await session.scalar(
                    select(DesktopContextSource).where(DesktopContextSource.parent_context_id == parent)
                )
                assert source is not None and source.context_id == child
            assert set(checkpointer.deleted_threads) == {parent_thread_id, leaf_thread_id}
        finally:
            await _cleanup(ws, [parent, child, leaf])
    _LOOP.run_until_complete(run())


def test_batch_cascade_delete_removes_whole_lineage_without_tombstone():
    async def run() -> None:
        checkpointer = FakeCheckpointer()
        contexts = _contexts(checkpointer)
        async with _SESSION_FACTORY() as session:
            ws = await _seed_workspace(session)
            parent = await _seed_thread(session, ws, "parent")
            child = await _seed_thread(session, ws, "child")
            await _link_child(session, child, parent)
            p_thread = (await session.get(DesktopThread, parent)).thread_id
            c_thread = (await session.get(DesktopThread, child)).thread_id
            await session.commit()
        try:
            await contexts.delete_many([parent], cascade=True)
            async with _SESSION_FACTORY() as session:
                assert await session.get(DesktopThread, parent) is None
                assert await session.get(DesktopThread, child) is None
            assert set(checkpointer.deleted_threads) == {p_thread, c_thread}
        finally:
            await _cleanup(ws, [parent, child])
    _LOOP.run_until_complete(run())


def test_batch_delete_rejects_whole_batch_on_active_run():
    async def run() -> None:
        contexts = _contexts()
        async with _SESSION_FACTORY() as session:
            ws = await _seed_workspace(session)
            a = await _seed_thread(session, ws, "a")
            b = await _seed_thread(session, ws, "b")
            await _seed_main_run(session, b, "running")
            await session.commit()
        try:
            try:
                await contexts.delete_many([a, b])
                raise AssertionError("批量删除应对运行中会话整批抛 409")
            except HTTPException as exc:
                assert exc.status_code == 409
            async with _SESSION_FACTORY() as session:
                assert await session.get(DesktopThread, a) is not None
                assert await session.get(DesktopThread, b) is not None
        finally:
            await _cleanup(ws, [a, b])
    _LOOP.run_until_complete(run())


def test_batch_delete_missing_id_returns_404_and_deletes_nothing():
    async def run() -> None:
        contexts = _contexts()
        async with _SESSION_FACTORY() as session:
            ws = await _seed_workspace(session)
            a = await _seed_thread(session, ws, "a")
            await session.commit()
        try:
            try:
                await contexts.delete_many([a, "ct-missing"])
                raise AssertionError("批量删除应对缺失会话抛 404")
            except HTTPException as exc:
                assert exc.status_code == 404
            async with _SESSION_FACTORY() as session:
                assert await session.get(DesktopThread, a) is not None
        finally:
            await _cleanup(ws, [a])
    _LOOP.run_until_complete(run())


def test_batch_delete_empty_list_returns_422():
    async def run() -> None:
        contexts = _contexts()
        try:
            await contexts.delete_many([])
            raise AssertionError("空列表应抛 422")
        except HTTPException as exc:
            assert exc.status_code == 422
    _LOOP.run_until_complete(run())
