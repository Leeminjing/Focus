"""Swarm 后台运行上下文与主 Agent 有界等待的回归测试。"""

import asyncio
import atexit
import json
import os
from types import SimpleNamespace
import uuid

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
_LOOP = asyncio.new_event_loop()
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

_ENGINE = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
_SESSION_FACTORY = async_sessionmaker(_ENGINE, expire_on_commit=False)
atexit.register(lambda: _LOOP.run_until_complete(_ENGINE.dispose()))

from backend.app.desktop.models import (  # noqa: E402
    AgentBoardTask,
    AgentMessage,
    DesktopRun,
    DesktopThread,
    DesktopWorkspace,
    SwarmAgent,
)
from backend.app.desktop.checkpoint_recovery import select_checkpoint_base  # noqa: E402
from backend.app.desktop.service import DesktopService  # noqa: E402
from focus.runtime.checkpointer.namespaced import NamespacedCheckpointer  # noqa: E402
from focus.runtime.runs.events import serialize_message, validate_messages  # noqa: E402
from focus.runtime.runs.manager import RunManager  # noqa: E402
from focus.runtime.runs.limits import DEFAULT_AGENT_RECURSION_LIMIT  # noqa: E402
from focus.runtime.runs.schemas import RunStatus  # noqa: E402
from focus.runtime.stream_bridge.memory import MemoryStreamBridge  # noqa: E402


def _runtime(task_id: str) -> ToolRuntime:
    return ToolRuntime(
        state={},
        context={"agent_id": f"main:{task_id}", "task_id": task_id},
        config={},
        stream_writer=None,
        tool_call_id=None,
        store=None,
        tools=[],
    )


async def _seed() -> tuple[str, str, str]:
    task_id = uuid.uuid4().hex
    workspace_id = uuid.uuid4().hex
    thread_id = uuid.uuid4().hex
    agent_id = uuid.uuid4().hex
    async with _SESSION_FACTORY() as session:
        session.add(DesktopWorkspace(
            workspace_id=workspace_id, path=f"C:/tmp/{workspace_id}", display_name="swarm-runtime-test"
        ))
        await session.flush()
        session.add(DesktopThread(
            task_id=task_id, workspace_id=workspace_id, thread_id=thread_id, title="swarm-runtime"
        ))
        await session.flush()
        session.add(SwarmAgent(
            agent_id=agent_id, task_id=task_id, role="worker",
            checkpoint_ns=f"swarm:{agent_id}", status="active", permissions=["read"],
        ))
        await session.commit()
    return task_id, workspace_id, agent_id


async def _cleanup(task_ids: list[str], workspace_id: str) -> None:
    async with _SESSION_FACTORY() as session:
        await session.execute(delete(AgentMessage).where(AgentMessage.task_id.in_(task_ids)))
        await session.execute(delete(AgentBoardTask).where(AgentBoardTask.thread_task_id.in_(task_ids)))
        await session.execute(delete(DesktopRun).where(DesktopRun.task_id.in_(task_ids)))
        await session.execute(delete(SwarmAgent).where(SwarmAgent.task_id.in_(task_ids)))
        await session.execute(delete(DesktopThread).where(DesktopThread.task_id.in_(task_ids)))
        await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id))
        await session.commit()


class _EmptyCheckpointer(SimpleNamespace):
    async def aget_tuple(self, config):
        return None


def _service(run_manager=None) -> DesktopService:
    return DesktopService(
        _SESSION_FACTORY,
        checkpointer=_EmptyCheckpointer(serde=object()),
        store=object(),
        bridge=object(),
        app_config=SimpleNamespace(models=[], commitment=SimpleNamespace(enabled=False)),
        run_manager=run_manager or SimpleNamespace(),
    )


def test_swarm_run_uses_clean_context_and_own_metadata(monkeypatch):
    """后台 graph 不继承 main 的 RunnableConfig，metadata 绑定新 DesktopRun。"""

    async def run() -> None:
        task_id, workspace_id, agent_id = await _seed()
        ambient_configs: list[object] = []
        runnable_configs: list[dict] = []

        class FakeRunManager:
            def __init__(self):
                self.record = None

            def create(self, *, thread_id, run_id, on_disconnect, model_name):
                self.record = SimpleNamespace(run_id=run_id, thread_id=thread_id, task=None)
                return self.record

        async def fake_run_agent(**kwargs):
            ambient_configs.append(var_child_runnable_config.get())
            runnable_configs.append(kwargs["runnable_config"])

        manager = FakeRunManager()
        service = _service(manager)
        monkeypatch.setattr("backend.app.desktop.service.run_agent", fake_run_agent)
        monkeypatch.setattr(service, "attach_run_sync", lambda record: None)
        monkeypatch.setattr(service, "_build_agent_factory", lambda *args: object())
        parent = {"metadata": {"run_id": "parent-main-run"}}
        token = var_child_runnable_config.set(parent)
        try:
            run_id = await service._launch_swarm_run(
                task_id, agent_id, "worker", "执行任务", "worker prompt",
                workspace_id, f"C:/tmp/{workspace_id}", {"permissions": ["read"]},
            )
            await manager.record.task
        finally:
            var_child_runnable_config.reset(token)
            await _cleanup([task_id], workspace_id)

        assert ambient_configs == [None]
        assert runnable_configs[0]["metadata"]["run_id"] == run_id
        assert runnable_configs[0]["configurable"]["run_id"] == run_id
        assert runnable_configs[0]["recursion_limit"] == DEFAULT_AGENT_RECURSION_LIMIT

    _LOOP.run_until_complete(run())


def test_second_swarm_run_applies_new_input_and_updates_checkpoint_lineage(monkeypatch):
    """同一 namespace 的第二轮是新输入，不得被当作首轮 stream 重连。"""

    async def run() -> None:
        task_id, workspace_id, agent_id = await _seed()
        saver = InMemorySaver()
        manager = RunManager()
        monkeypatch.setattr(manager, "_cleanup_later", lambda run_id: None)
        service = DesktopService(
            _SESSION_FACTORY,
            checkpointer=saver,
            store=None,
            bridge=MemoryStreamBridge(),
            app_config=SimpleNamespace(models=[], commitment=SimpleNamespace(enabled=False)),
            run_manager=manager,
        )
        seen: list[str] = []

        async def respond(state: MessagesState):
            seen.append(state["messages"][-1].content)
            return {"messages": [AIMessage(content=f"ack:{seen[-1]}")]}

        builder = StateGraph(MessagesState)
        builder.add_node("respond", respond)
        builder.add_edge(START, "respond")
        builder.add_edge("respond", END)
        graph = builder.compile()

        async def graph_factory():
            return graph

        monkeypatch.setattr(
            service, "_build_agent_factory", lambda *args, **kwargs: graph_factory
        )
        monkeypatch.setattr(service, "attach_run_sync", lambda record: None)
        try:
            first_run = await service._launch_swarm_run(
                task_id, agent_id, "worker", "first", "worker prompt",
                workspace_id, f"C:/tmp/{workspace_id}", {"permissions": ["read"]},
            )
            await manager.get(first_run).task
            second_run = await service._launch_swarm_run(
                task_id, agent_id, "worker", "second", "worker prompt",
                workspace_id, f"C:/tmp/{workspace_id}", {"permissions": ["read"]},
            )
            await manager.get(second_run).task

            namespaced = NamespacedCheckpointer(saver, f"swarm:{agent_id}")
            async with _SESSION_FACTORY() as session:
                thread_id = (await session.get(DesktopThread, task_id)).thread_id
            checkpoint = await namespaced.aget_tuple({
                "configurable": {"thread_id": thread_id}
            })
            assert seen == ["first", "second"]
            assert checkpoint.metadata["run_id"] == second_run
            contents = [message.content for message in checkpoint.checkpoint["channel_values"]["messages"]]
            assert contents == ["first", "ack:first", "second", "ack:second"]
        finally:
            await _cleanup([task_id], workspace_id)

    _LOOP.run_until_complete(run())


def test_swarm_run_recovers_from_latest_invalid_checkpoint(monkeypatch):
    """持久 Worker 再次启动时不得把未闭合工具调用发送给模型。"""

    async def run() -> None:
        task_id, workspace_id, agent_id = await _seed()
        saver = InMemorySaver()
        manager = RunManager()
        monkeypatch.setattr(manager, "_cleanup_later", lambda run_id: None)
        service = DesktopService(
            _SESSION_FACTORY,
            checkpointer=saver,
            store=None,
            bridge=MemoryStreamBridge(),
            app_config=SimpleNamespace(models=[], commitment=SimpleNamespace(enabled=False)),
            run_manager=manager,
        )
        seen: list[str] = []

        async def agent_turn(state: MessagesState):
            if state["messages"][-1].content == "first":
                return {"messages": [AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "list_files",
                        "args": {"path": "solution.slnx"},
                        "id": "call-unclosed",
                        "type": "tool_call",
                    }],
                )]}
            validate_messages([serialize_message(message) for message in state["messages"]])
            seen.append(state["messages"][-1].content)
            return {"messages": [AIMessage(content="ack:second")]}

        builder = StateGraph(MessagesState)
        builder.add_node("agent_turn", agent_turn)
        builder.add_edge(START, "agent_turn")
        builder.add_edge("agent_turn", END)
        graph = builder.compile()

        async def graph_factory():
            return graph

        monkeypatch.setattr(
            service, "_build_agent_factory", lambda *args, **kwargs: graph_factory
        )
        monkeypatch.setattr(service, "attach_run_sync", lambda record: None)
        try:
            first_run = await service._launch_swarm_run(
                task_id, agent_id, "worker", "first", "worker prompt",
                workspace_id, f"C:/tmp/{workspace_id}", {"permissions": ["read"]},
            )
            await manager.get(first_run).task
            assert manager.get(first_run).status == RunStatus.success

            async with _SESSION_FACTORY() as session:
                thread_id = (await session.get(DesktopThread, task_id)).thread_id
            recovery_base = await select_checkpoint_base(
                saver, thread_id, f"swarm:{agent_id}"
            )
            assert recovery_base is not None

            second_run = await service._launch_swarm_run(
                task_id, agent_id, "worker", "second", "worker prompt",
                workspace_id, f"C:/tmp/{workspace_id}", {"permissions": ["read"]},
            )
            await manager.get(second_run).task

            assert manager.get(second_run).status == RunStatus.success
            assert seen == ["second"]
            namespaced = NamespacedCheckpointer(saver, f"swarm:{agent_id}")
            checkpoint = await namespaced.aget_tuple({
                "configurable": {"thread_id": thread_id}
            })
            contents = [
                message.content
                for message in checkpoint.checkpoint["channel_values"]["messages"]
            ]
            assert contents == ["first", "second", "ack:second"]
        finally:
            await _cleanup([task_id], workspace_id)

    _LOOP.run_until_complete(run())


def test_wait_for_swarm_returns_terminal_snapshot_without_consuming_messages():
    async def run() -> None:
        task_id, workspace_id, agent_id = await _seed()
        run_id = uuid.uuid4().hex
        message_id = uuid.uuid4().hex
        board_id = uuid.uuid4().hex
        service = _service()
        wait_tool = next(tool for tool in service._build_swarm_tools() if tool.name == "wait_for_swarm")
        try:
            async with _SESSION_FACTORY() as session:
                session.add(DesktopRun(
                    run_id=run_id, task_id=task_id, agent_id=agent_id,
                    kind="worker", status="pending", input_messages=[],
                ))
                session.add(AgentBoardTask(
                    board_task_id=board_id, thread_task_id=task_id,
                    description="计算 8*7", status="claimed", claimed_by=agent_id,
                ))
                await session.commit()

            async def finish() -> None:
                await asyncio.sleep(0.1)
                async with _SESSION_FACTORY() as session:
                    await session.execute(
                        update(DesktopRun).where(DesktopRun.run_id == run_id).values(status="success")
                    )
                    await session.execute(
                        update(AgentBoardTask).where(AgentBoardTask.board_task_id == board_id)
                        .values(status="completed", result="56")
                    )
                    session.add(AgentMessage(
                        message_id=message_id, task_id=task_id, from_agent=agent_id,
                        to_agent=f"main:{task_id}", kind="message", content="结果是 56",
                    ))
                    await session.commit()

            updater = asyncio.create_task(finish())
            payload = json.loads(await wait_tool.ainvoke({
                "agent_ids": [agent_id], "timeout_seconds": 2, "runtime": _runtime(task_id),
            }))
            await updater
            assert payload["timed_out"] is False
            assert payload["agents"] == [{"agent_id": agent_id, "run_id": run_id, "status": "success"}]
            assert payload["board_tasks"][0]["status"] == "completed"
            assert payload["unread_messages"][0]["content"] == "结果是 56"
            async with _SESSION_FACTORY() as session:
                assert await session.scalar(
                    select(AgentMessage.read_at).where(AgentMessage.message_id == message_id)
                ) is None
        finally:
            await _cleanup([task_id], workspace_id)

    _LOOP.run_until_complete(run())


def test_wait_for_swarm_returns_on_protocol_message_without_consuming_it():
    async def run() -> None:
        task_id, workspace_id, agent_id = await _seed()
        run_id = uuid.uuid4().hex
        message_id = uuid.uuid4().hex
        service = _service()
        wait_tool = next(tool for tool in service._build_swarm_tools() if tool.name == "wait_for_swarm")
        try:
            async with _SESSION_FACTORY() as session:
                session.add(DesktopRun(
                    run_id=run_id, task_id=task_id, agent_id=agent_id,
                    kind="worker", status="running", input_messages=[],
                ))
                await session.commit()

            async def request_approval() -> None:
                await asyncio.sleep(0.1)
                async with _SESSION_FACTORY() as session:
                    session.add(AgentMessage(
                        message_id=message_id, task_id=task_id, from_agent=agent_id,
                        to_agent=f"main:{task_id}", kind="plan_approval_request", content="请批准计划",
                    ))
                    await session.commit()

            updater = asyncio.create_task(request_approval())
            payload = json.loads(await wait_tool.ainvoke({
                "agent_ids": [agent_id], "timeout_seconds": 2, "runtime": _runtime(task_id),
            }))
            await updater
            assert payload["timed_out"] is False
            assert payload["unread_messages"][0]["kind"] == "plan_approval_request"
            async with _SESSION_FACTORY() as session:
                assert await session.scalar(
                    select(AgentMessage.read_at).where(AgentMessage.message_id == message_id)
                ) is None
        finally:
            await _cleanup([task_id], workspace_id)

    _LOOP.run_until_complete(run())


def test_wait_for_swarm_ignores_stale_unread_message_until_timeout():
    async def run() -> None:
        task_id, workspace_id, agent_id = await _seed()
        message_id = uuid.uuid4().hex
        service = _service()
        wait_tool = next(tool for tool in service._build_swarm_tools() if tool.name == "wait_for_swarm")
        try:
            async with _SESSION_FACTORY() as session:
                session.add(DesktopRun(
                    run_id=uuid.uuid4().hex, task_id=task_id, agent_id=agent_id,
                    kind="worker", status="running", input_messages=[],
                ))
                session.add(AgentMessage(
                    message_id=message_id, task_id=task_id, from_agent=agent_id,
                    to_agent=f"main:{task_id}", kind="message", content="旧消息",
                ))
                await session.commit()

            started = asyncio.get_running_loop().time()
            payload = json.loads(await wait_tool.ainvoke({
                "agent_ids": [agent_id], "timeout_seconds": 1, "runtime": _runtime(task_id),
            }))
            elapsed = asyncio.get_running_loop().time() - started

            assert payload["timed_out"] is True
            assert elapsed >= 0.9
            assert payload["unread_messages"][0]["message_id"] == message_id
            async with _SESSION_FACTORY() as session:
                assert await session.scalar(
                    select(AgentMessage.read_at).where(AgentMessage.message_id == message_id)
                ) is None
        finally:
            await _cleanup([task_id], workspace_id)

    _LOOP.run_until_complete(run())


def test_wait_for_swarm_returns_on_new_message_after_stale_unread_baseline():
    async def run() -> None:
        task_id, workspace_id, agent_id = await _seed()
        stale_id = uuid.uuid4().hex
        new_id = uuid.uuid4().hex
        service = _service()
        wait_tool = next(tool for tool in service._build_swarm_tools() if tool.name == "wait_for_swarm")
        try:
            async with _SESSION_FACTORY() as session:
                session.add(DesktopRun(
                    run_id=uuid.uuid4().hex, task_id=task_id, agent_id=agent_id,
                    kind="worker", status="running", input_messages=[],
                ))
                session.add(AgentMessage(
                    message_id=stale_id, task_id=task_id, from_agent=agent_id,
                    to_agent=f"main:{task_id}", kind="message", content="旧消息",
                ))
                await session.commit()

            async def send_new_message() -> None:
                await asyncio.sleep(0.1)
                async with _SESSION_FACTORY() as session:
                    session.add(AgentMessage(
                        message_id=new_id, task_id=task_id, from_agent=agent_id,
                        to_agent=f"main:{task_id}", kind="message", content="新消息",
                    ))
                    await session.commit()

            updater = asyncio.create_task(send_new_message())
            payload = json.loads(await wait_tool.ainvoke({
                "agent_ids": [agent_id], "timeout_seconds": 2, "runtime": _runtime(task_id),
            }))
            await updater

            assert payload["timed_out"] is False
            assert {message["message_id"] for message in payload["unread_messages"]} == {
                stale_id, new_id,
            }
            async with _SESSION_FACTORY() as session:
                assert (await session.scalars(
                    select(AgentMessage.read_at).where(AgentMessage.message_id.in_([stale_id, new_id]))
                )).all() == [None, None]
        finally:
            await _cleanup([task_id], workspace_id)

    _LOOP.run_until_complete(run())


def test_wait_for_swarm_timeout_and_validation_errors():
    async def run() -> None:
        task_id, workspace_id, agent_id = await _seed()
        other_task = uuid.uuid4().hex
        other_agent = uuid.uuid4().hex
        service = _service()
        wait_tool = next(tool for tool in service._build_swarm_tools() if tool.name == "wait_for_swarm")
        try:
            async with _SESSION_FACTORY() as session:
                session.add(DesktopThread(
                    task_id=other_task, workspace_id=workspace_id,
                    thread_id=uuid.uuid4().hex, title="other-task",
                ))
                await session.flush()
                session.add(SwarmAgent(
                    agent_id=other_agent, task_id=other_task, role="teammate",
                    checkpoint_ns=f"swarm:{other_agent}", status="active", permissions=["read"],
                ))
                session.add(DesktopRun(
                    run_id=uuid.uuid4().hex, task_id=task_id, agent_id=agent_id,
                    kind="worker", status="running", input_messages=[],
                ))
                await session.commit()

            payload = json.loads(await wait_tool.ainvoke({
                "agent_ids": [agent_id], "timeout_seconds": 1, "runtime": _runtime(task_id),
            }))
            assert payload["timed_out"] is True

            for ids, timeout in [([], 1), (["missing"], 1), ([other_agent], 1), ([agent_id], 0), ([agent_id], 31)]:
                try:
                    await wait_tool.ainvoke({
                        "agent_ids": ids, "timeout_seconds": timeout, "runtime": _runtime(task_id),
                    })
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"expected ValueError: ids={ids!r}, timeout={timeout}")
        finally:
            await _cleanup([task_id, other_task], workspace_id)

    _LOOP.run_until_complete(run())
