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

from focus.config.app_config import AppConfig  # noqa: E402

from backend.app.desktop.contexts.curation.models import PortfolioRevision
from backend.app.desktop.contexts.evolution import (
    ContextRevisionRepository,
    ContextRevisionSourceContract,
)
from backend.app.desktop.contexts.evolution.history import build_revision_history
from backend.app.desktop.contexts.evolution.models import (  # noqa: E402
    ContextInputSnapshot,
    ContextRevision,
)
from backend.app.desktop.contexts.evolution.reader import ContextRevisionReader
from backend.app.desktop.contexts.service import ContextService  # noqa: E402
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopRound
from backend.app.desktop.persistence.models import (
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
    PatrolAgent,
    PatrolContextBinding,
    PatrolContextRevision,
)


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
    parent = await _ensure_revision(session, parent_task_id)
    child = await _ensure_revision(session, child_task_id)
    source = await ContextRevisionRepository().get_by_id(session, parent.revision_id)
    child.sources = [ContextRevisionSourceContract(source=source.ref, position=0).model_dump(mode="json")]
    await session.flush()


async def _ensure_revision(session, context_id: str) -> ContextRevision:
    current = await session.scalar(
        select(ContextRevision).where(ContextRevision.context_id == context_id)
    )
    if current is not None:
        return current
    task = await session.get(DesktopThread, context_id)
    revision = ContextRevision(
        revision_id=uuid.uuid4().hex,
        context_id=context_id,
        generation=1,
        execution_thread_id=task.thread_id,
        checkpoint_ns="",
        checkpoint_id=f"checkpoint-{context_id}",
        payload_mode="checkpoint",
        content_hash=uuid.uuid4().hex * 2,
        projection_status="valid",
        origin_kind="root",
    )
    session.add(revision)
    await session.flush()
    task.current_revision_id = revision.revision_id
    await session.flush()
    return revision


async def _seed_main_run(session, task_id: str, status: str = "pending") -> None:
    session.add(DesktopRun(
        run_id=f"run-{uuid.uuid4().hex[:10]}", task_id=task_id,
        agent_id=f"main:{task_id}", kind="main", status=status, input_messages=[],
    ))
    await session.flush()


async def _cleanup(workspace_id: str, task_ids: list[str]) -> None:
    async with _SESSION_FACTORY() as session:
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


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("borrowed_by_round", [False, True])
@pytest.mark.parametrize("with_descendant", [False, True])
def test_delete_releases_destroyed_owners_and_preserves_other_round_input(batch, borrowed_by_round, with_descendant):
    async def run() -> None:
        contexts = _contexts()
        repository = ContextRevisionRepository()
        run_id, curator_id, round_id, loop_id, portfolio_id = (uuid.uuid4().hex for _ in range(5))
        async with _SESSION_FACTORY.begin() as session:
            workspace_id = await _seed_workspace(session)
            deleted_id = await _seed_thread(session, workspace_id, "delete")
            keeper_id = await _seed_thread(session, workspace_id, "keeper")
            if with_descendant:
                await _link_child(session, keeper_id, deleted_id)
            deleted = await _ensure_revision(session, deleted_id)
            deleted.history_payload = build_revision_history([], [{
                "role": "human", "id": "frozen", "content": "borrowed frozen input",
            }]).model_dump(mode="json")
            await _ensure_revision(session, keeper_id)
            await session.flush()
            deleted_ref = (await repository.current(session, deleted_id)).ref
            keeper_ref = (await repository.current(session, keeper_id)).ref
            session.add(DesktopRun(run_id=run_id, task_id=deleted_id, agent_id=f"main:{deleted_id}",
                                   kind="main", status="interrupted", context_revision_id=keeper_ref.revision_id))
            agent_id, binding_id = uuid.uuid4().hex, uuid.uuid4().hex
            session.add(PatrolAgent(agent_id=agent_id, task_id=deleted_id,
                                    checkpoint_ns=agent_id, system_prompt="", frozen_messages=[],
                                    equipment={}, mode="context_curator"))
            await session.flush()
            session.add(PatrolContextBinding(binding_id=binding_id, agent_id=agent_id,
                                             root_context_id=deleted_id, control_state="stopped"))
            await session.flush()
            session.add(PatrolContextRevision(revision_id=curator_id, binding_id=binding_id,
                                              source_checkpoint_id=deleted_ref.checkpoint_id,
                                              base_binding_revision=0, status="interrupted"))
            await session.flush()
            binding = await session.get(PatrolContextBinding, binding_id)
            program = await contexts.single_lane_programs.provision(session, binding)
            session.add(PortfolioRevision(
                portfolio_revision_id=portfolio_id, program_id=program.program_id,
                generation=1, frontier_hash="a" * 64, base_program_revision=0, status="preparing",
            ))
            await session.flush()
            await repository.freeze(session, keeper_ref, owner_kind="run", owner_id=run_id)
            await repository.freeze(session, deleted_ref, owner_kind="context_curator", owner_id=curator_id)
            await repository.freeze(session, deleted_ref, owner_kind="curation_portfolio", owner_id=portfolio_id)
            if borrowed_by_round:
                session.add(AgentLoop(loop_id=loop_id, workspace_id=workspace_id,
                                      initial_context_id=keeper_id, holder_id="keeper", status="running"))
                await session.flush()
                session.add(LoopRound(round_id=round_id, loop_id=loop_id, number=1,
                                      authority_revision=1, goal_revision=1, frontier_hash="a" * 64,
                                      workspace_revision=1, status="observed"))
                await session.flush()
                await repository.freeze(session, deleted_ref, owner_kind="round", owner_id=round_id)
        try:
            if batch:
                await contexts.delete_many([deleted_id])
            else:
                await contexts.delete(deleted_id)
            async with _SESSION_FACTORY() as session:
                deleted_context = await session.get(DesktopThread, deleted_id)
                if with_descendant:
                    assert deleted_context is not None and deleted_context.deleted_at is not None
                    assert await session.get(DesktopThread, keeper_id) is not None
                else:
                    assert deleted_context is None
                assert await session.get(DesktopRun, run_id) is None
                assert await session.get(PatrolContextRevision, curator_id) is None
                assert await session.get(PortfolioRevision, portfolio_id) is None
                remaining = list((await session.scalars(select(ContextInputSnapshot).where(
                    ContextInputSnapshot.owner_id.in_((run_id, curator_id, round_id, portfolio_id))
                ))).all())
                assert [(snapshot.owner_kind, snapshot.owner_id) for snapshot in remaining] == (
                    [("round", round_id)] if borrowed_by_round else []
                )
                if borrowed_by_round:
                    assert await session.get(LoopRound, round_id) is not None
                    view = await ContextRevisionReader(repository, contexts.checkpointer).read(session, deleted_ref, "display")
                    assert [message["content"] for message in view.messages] == ["borrowed frozen input"]
                else:
                    assert await session.get(ContextRevision, deleted_ref.revision_id) is None
            async with _SESSION_FACTORY.begin() as session:
                await repository.release(session, "round", round_id)
                assert await session.get(ContextRevision, deleted_ref.revision_id) is None
        finally:
            async with _SESSION_FACTORY.begin() as session:
                await repository.release_owners(session, (("run", run_id), ("context_curator", curator_id),
                                                         ("round", round_id), ("curation_portfolio", portfolio_id)))
                await session.execute(delete(AgentLoop).where(AgentLoop.loop_id == loop_id))
            await _cleanup(workspace_id, [deleted_id, keeper_id])

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
                target = await session.scalar(select(ContextRevision).where(ContextRevision.context_id == child))
                assert len(target.sources) == 1
                assert target.sources[0]["source"]["context_id"] == parent
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
                target = await session.scalar(select(ContextRevision).where(ContextRevision.context_id == child))
                assert len(target.sources) == 1
                assert target.sources[0]["source"]["context_id"] == parent
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
