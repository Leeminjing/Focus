"""活跃执行视图测试：任务页会话在结算未可见时与执行身份同源，结算可见后与已发布 revision 一致。

测试复用项目既有模式（真实 Postgres engine + 独立 loop，不触碰全局单例）。
"""

import asyncio
import atexit
import os
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import CheckpointTuple
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
_LOOP = asyncio.new_event_loop()

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

_ENGINE = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
_SESSION_FACTORY = async_sessionmaker(_ENGINE, expire_on_commit=False)
atexit.register(lambda: _LOOP.run_until_complete(_ENGINE.dispose()))

from backend.app.desktop.context_service import ContextService  # noqa: E402
from backend.app.desktop.context_evolution.models import ContextRevision  # noqa: E402
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace  # noqa: E402
from focus.config.app_config import AppConfig  # noqa: E402


class ScriptedCheckpointer:
    """按执行身份保存有序 checkpoint：带 checkpoint_id 的读取精确命中，不带则返回最新。"""

    def __init__(self) -> None:
        self._by_identity: dict[tuple[str, str], list[CheckpointTuple]] = {}
        self.reads: list[dict] = []

    def append(self, thread_id: str, namespace: str, checkpoint_id: str, messages: list) -> None:
        self._by_identity.setdefault((thread_id, namespace), []).append(
            CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": namespace,
                        "checkpoint_id": checkpoint_id,
                    }
                },
                checkpoint={"channel_values": {"messages": list(messages)}},
                metadata={},
                parent_config=None,
            )
        )

    async def aget_tuple(self, config):
        self.reads.append(config)
        configurable = config.get("configurable", {})
        candidates = self._by_identity.get(
            (configurable.get("thread_id"), configurable.get("checkpoint_ns", ""))
        ) or []
        wanted = configurable.get("checkpoint_id")
        if wanted:
            return next(
                (item for item in candidates if _checkpoint_id(item) == wanted),
                None,
            )
        return candidates[-1] if candidates else None

    async def adelete_thread(self, thread_id: str) -> None:
        return None


def _checkpoint_id(checkpoint: CheckpointTuple) -> str | None:
    return checkpoint.config.get("configurable", {}).get("checkpoint_id")


def _app_config() -> AppConfig:
    return AppConfig.model_validate({
        "models": [{
            "name": "deepseek-v4-flash", "display_name": "m", "use": "x:y",
            "model": "deepseek-v4-flash", "api_key": "k", "base_url": "http://x",
            "context_window": 131072,
        }]
    })


def _contexts(checkpointer: ScriptedCheckpointer) -> ContextService:
    return ContextService(_SESSION_FACTORY, checkpointer, _app_config())


def _published_messages() -> list:
    return [HumanMessage(content="你好", id="m-1"), AIMessage(content="问候", id="m-2")]


def _newer_messages() -> list:
    return [
        *_published_messages(),
        HumanMessage(content="【任务目标】修复", id="m-3"),
        AIMessage(content="我先勘察工作区", id="m-4"),
    ]


async def _seed(session) -> tuple[str, str]:
    workspace_id = f"ws-{uuid.uuid4().hex[:14]}"
    task_id = f"ct-{uuid.uuid4().hex[:14]}"
    session.add(DesktopWorkspace(workspace_id=workspace_id, path=f"/tmp/{workspace_id}", display_name="t"))
    await session.flush()
    session.add(DesktopThread(
        task_id=task_id, workspace_id=workspace_id,
        thread_id=f"th-{uuid.uuid4().hex[:10]}", title="t",
    ))
    await session.flush()
    return workspace_id, task_id


async def _publish_revision(session, task_id: str, execution_thread_id: str, checkpoint_id: str, generation: int) -> ContextRevision:
    revision = ContextRevision(
        revision_id=uuid.uuid4().hex,
        context_id=task_id,
        generation=generation,
        execution_thread_id=execution_thread_id,
        checkpoint_ns="",
        checkpoint_id=checkpoint_id,
        payload_mode="checkpoint",
        content_hash=uuid.uuid4().hex * 2,
        projection_status="valid",
        origin_kind="run_settled",
    )
    session.add(revision)
    await session.flush()
    task = await session.get(DesktopThread, task_id)
    task.current_revision_id = revision.revision_id
    await session.flush()
    return revision


async def _cleanup(workspace_id: str, task_id: str) -> None:
    async with _SESSION_FACTORY() as session:
        await session.execute(delete(ContextRevision).where(ContextRevision.context_id == task_id))
        await session.execute(delete(DesktopThread).where(DesktopThread.task_id == task_id))
        await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id))
        await session.commit()


def _ids(messages: list[dict]) -> list[str]:
    return [str(message.get("id")) for message in messages]


def test_live_view_matches_published_when_nothing_newer():
    async def run() -> None:
        checkpointer = ScriptedCheckpointer()
        contexts = _contexts(checkpointer)
        async with _SESSION_FACTORY() as session:
            workspace_id, task_id = await _seed(session)
            thread_id = (await session.get(DesktopThread, task_id)).thread_id
            await _publish_revision(session, task_id, thread_id, "ck-1", 1)
            await session.commit()
        checkpointer.append(thread_id, "", "ck-1", _published_messages())
        try:
            published = await contexts.snapshot(task_id)
            live = await contexts.live_conversation(task_id)
            assert live == published
            assert _ids(live["messages"]) == ["m-1", "m-2"]
        finally:
            await _cleanup(workspace_id, task_id)

    _LOOP.run_until_complete(run())


def test_live_view_returns_newer_execution_state_without_moving_published_view():
    async def run() -> None:
        checkpointer = ScriptedCheckpointer()
        contexts = _contexts(checkpointer)
        async with _SESSION_FACTORY() as session:
            workspace_id, task_id = await _seed(session)
            thread_id = (await session.get(DesktopThread, task_id)).thread_id
            await _publish_revision(session, task_id, thread_id, "ck-1", 1)
            await session.commit()
        checkpointer.append(thread_id, "", "ck-1", _published_messages())
        checkpointer.append(thread_id, "", "ck-2", _newer_messages())
        try:
            live = await contexts.live_conversation(task_id)
            published = await contexts.snapshot(task_id)
            assert _ids(live["messages"]) == ["m-1", "m-2", "m-3", "m-4"]
            assert live["checkpoint_id"] == "ck-2"
            assert _ids(published["messages"]) == ["m-1", "m-2"]
            assert published["checkpoint_id"] == "ck-1"
        finally:
            await _cleanup(workspace_id, task_id)

    _LOOP.run_until_complete(run())


def test_live_view_is_read_only():
    async def run() -> None:
        checkpointer = ScriptedCheckpointer()
        contexts = _contexts(checkpointer)
        async with _SESSION_FACTORY() as session:
            workspace_id, task_id = await _seed(session)
            thread_id = (await session.get(DesktopThread, task_id)).thread_id
            revision = await _publish_revision(session, task_id, thread_id, "ck-1", 1)
            published_revision_id = revision.revision_id
            await session.commit()
        checkpointer.append(thread_id, "", "ck-1", _published_messages())
        checkpointer.append(thread_id, "", "ck-2", _newer_messages())
        try:
            await contexts.live_conversation(task_id)
            async with _SESSION_FACTORY() as session:
                revision_count = await session.scalar(
                    select(func.count()).select_from(ContextRevision).where(ContextRevision.context_id == task_id)
                )
                task = await session.get(DesktopThread, task_id)
                assert revision_count == 1
                assert task.current_revision_id == published_revision_id
        finally:
            await _cleanup(workspace_id, task_id)

    _LOOP.run_until_complete(run())


def test_post_stream_refresh_before_settlement_returns_newer_state_without_active_run():
    async def run() -> None:
        checkpointer = ScriptedCheckpointer()
        contexts = _contexts(checkpointer)
        async with _SESSION_FACTORY() as session:
            workspace_id, task_id = await _seed(session)
            thread_id = (await session.get(DesktopThread, task_id)).thread_id
            await _publish_revision(session, task_id, thread_id, "ck-1", 1)
            await session.commit()
        checkpointer.append(thread_id, "", "ck-1", _published_messages())
        checkpointer.append(thread_id, "", "ck-2", _newer_messages())
        try:
            live = await contexts.live_conversation(task_id)
            async with _SESSION_FACTORY() as session:
                active_runs = await session.scalar(
                    select(func.count()).select_from(DesktopRun).where(
                        DesktopRun.task_id == task_id,
                        DesktopRun.status.in_(["pending", "running"]),
                    )
                )
            assert active_runs == 0
            assert _ids(live["messages"]) == ["m-1", "m-2", "m-3", "m-4"]
        finally:
            await _cleanup(workspace_id, task_id)

    _LOOP.run_until_complete(run())


def test_view_transitions_continuously_across_settle_and_next_run():
    async def run() -> None:
        checkpointer = ScriptedCheckpointer()
        contexts = _contexts(checkpointer)
        next_round = [*_newer_messages(), AIMessage(content="下一轮开始", id="m-5")]
        async with _SESSION_FACTORY() as session:
            workspace_id, task_id = await _seed(session)
            thread_id = (await session.get(DesktopThread, task_id)).thread_id
            await _publish_revision(session, task_id, thread_id, "ck-2", 1)
            await session.commit()
        checkpointer.append(thread_id, "", "ck-2", _newer_messages())
        try:
            settled = await contexts.live_conversation(task_id)
            assert _ids(settled["messages"]) == ["m-1", "m-2", "m-3", "m-4"]
            checkpointer.append(thread_id, "", "ck-3", next_round)
            advanced = await contexts.live_conversation(task_id)
            assert _ids(advanced["messages"])[: len(_ids(settled["messages"]))] == _ids(settled["messages"])
            assert _ids(advanced["messages"])[-1] == "m-5"
        finally:
            await _cleanup(workspace_id, task_id)

    _LOOP.run_until_complete(run())


def test_live_view_converges_on_published_revision_after_settlement():
    async def run() -> None:
        checkpointer = ScriptedCheckpointer()
        contexts = _contexts(checkpointer)
        async with _SESSION_FACTORY() as session:
            workspace_id, task_id = await _seed(session)
            thread_id = (await session.get(DesktopThread, task_id)).thread_id
            await _publish_revision(session, task_id, thread_id, "ck-1", 1)
            await session.commit()
        checkpointer.append(thread_id, "", "ck-1", _published_messages())
        checkpointer.append(thread_id, "", "ck-2", _newer_messages())
        try:
            before_settlement = await contexts.live_conversation(task_id)
            async with _SESSION_FACTORY() as session:
                await _publish_revision(session, task_id, thread_id, "ck-2", 2)
                await session.commit()
            after_settlement = await contexts.live_conversation(task_id)
            published = await contexts.snapshot(task_id)
            assert _ids(before_settlement["messages"]) == _ids(after_settlement["messages"])
            assert _ids(after_settlement["messages"]) == _ids(published["messages"])
            assert after_settlement["checkpoint_id"] == "ck-2"
        finally:
            await _cleanup(workspace_id, task_id)

    _LOOP.run_until_complete(run())
