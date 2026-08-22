"""Agent 工具调用韧性测试：错误 ToolMessage、历史读取边界与 checkpoint 恢复。

输入为纯内存 ToolCallRequest/checkpoint tuple，以及最小 gateway request fake；输出为工具消息
关联、合法祖先选择和 RunnableConfig 透传断言。具体工作流为：先独立调用 middleware，
再以并行结果运行仓库 validate_messages，随后验证 Swarm/Patrol 历史工具边界，最后验证
checkpoint preflight 与 start_run 的 checkpoint_id 透传。示例：
`pytest backend/tests/test_agent_tool_call_resilience.py -q`。
"""

import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import ToolException

os.environ.setdefault("OPENAI_API_KEY", "desktop-test")

from backend.app.desktop.checkpoint_recovery import (  # noqa: E402
    EMPTY_CHECKPOINT_ID,
    select_checkpoint_base,
)
from backend.app.desktop.service import DesktopService  # noqa: E402
from backend.app.desktop.tool_error_provider import build_tool_error_middleware  # noqa: E402
from backend.app.gateway.routers.thread_runs import RunCreateRequest  # noqa: E402
from focus.runtime.runs.events import serialize_message, validate_messages  # noqa: E402


def _runtime() -> ToolRuntime:
    return ToolRuntime(
        state={}, context={}, config={}, stream_writer=None,
        tool_call_id=None, store=None, tools=[],
    )


def _request(call_id: str) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": "test_tool", "args": {}, "id": call_id, "type": "tool_call"},
        tool=None,
        state={},
        runtime=_runtime(),
    )


def test_tool_error_middleware_classifies_recoverable_errors():
    async def run() -> None:
        middleware = build_tool_error_middleware()

        async def value_error(_request):
            raise ValueError("参数不属于当前任务")

        result = await middleware.awrap_tool_call(_request("call-value"), value_error)
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert result.tool_call_id == "call-value"
        assert "参数不属于当前任务" in str(result.content)

        async def client_error(_request):
            raise HTTPException(409, "该 Agent 已停止")

        result = await middleware.awrap_tool_call(_request("call-409"), client_error)
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert result.tool_call_id == "call-409"
        assert "该 Agent 已停止" in str(result.content)

        for call_id, error in (
            ("call-missing", FileNotFoundError("missing.txt")),
            ("call-not-directory", NotADirectoryError("solution.slnx")),
            ("call-is-directory", IsADirectoryError("src")),
        ):
            async def path_input_error(_request, error=error):
                raise error

            result = await middleware.awrap_tool_call(_request(call_id), path_input_error)
            assert isinstance(result, ToolMessage)
            assert result.status == "error"
            assert result.tool_call_id == call_id
            assert str(error) in str(result.content)

        async def permission_error(_request):
            raise PermissionError("workspace denied")

        with pytest.raises(PermissionError, match="workspace denied"):
            await middleware.awrap_tool_call(_request("call-permission"), permission_error)

        async def programming_error(_request):
            raise RuntimeError("implementation bug")

        with pytest.raises(RuntimeError, match="implementation bug"):
            await middleware.awrap_tool_call(_request("call-runtime"), programming_error)

        async def server_error(_request):
            raise HTTPException(503, "database unavailable")

        with pytest.raises(HTTPException) as exc_info:
            await middleware.awrap_tool_call(_request("call-503"), server_error)
        assert exc_info.value.status_code == 503

    asyncio.run(run())


def test_parallel_tool_results_remain_message_complete():
    async def run() -> None:
        middleware = build_tool_error_middleware()

        async def fail(_request):
            raise ToolException("路径不属于当前工作区: ../outside.txt")

        async def succeed(request):
            return ToolMessage(content="[]", tool_call_id=request.tool_call["id"])

        failed, succeeded = await asyncio.gather(
            middleware.awrap_tool_call(_request("call-fail"), fail),
            middleware.awrap_tool_call(_request("call-ok"), succeed),
        )
        messages = [
            serialize_message(AIMessage(content="", tool_calls=[
                {"name": "read_patrol_agent_history", "args": {}, "id": "call-fail", "type": "tool_call"},
                {"name": "list_board_tasks", "args": {}, "id": "call-ok", "type": "tool_call"},
            ])),
            serialize_message(failed),
            serialize_message(succeeded),
        ]
        validate_messages(messages)
        assert {messages[1]["tool_call_id"], messages[2]["tool_call_id"]} == {"call-fail", "call-ok"}
        assert messages[1]["status"] == "error"

        async def next_model(history):
            assert history[-2].tool_call_id == "call-fail"
            assert history[-2].status == "error"
            assert history[-1].tool_call_id == "call-ok"
            return AIMessage(content="越界路径不可用，我将改用工作区内文件。")

        continuation = await next_model([failed, succeeded])
        assert "改用工作区内" in continuation.content

    asyncio.run(run())


def test_swarm_and_patrol_history_readers_keep_entity_boundaries():
    async def run() -> None:
        service = object.__new__(DesktopService)
        current = SimpleNamespace(
            agent_id="swarm-current", task_id="task-a", checkpoint_ns="swarm:swarm-current"
        )
        foreign = SimpleNamespace(
            agent_id="swarm-foreign", task_id="task-b", checkpoint_ns="swarm:swarm-foreign"
        )

        class FakeCollab:
            async def get_swarm_agent(self, agent_id):
                return {"swarm-current": current, "swarm-foreign": foreign}.get(agent_id)

        service.agent_collab = FakeCollab()

        async def get_checkpoint_messages(thread_id, checkpoint_ns):
            assert thread_id == "thread-a"
            assert checkpoint_ns == "swarm:swarm-current"
            return [{"role": "human", "content": "协作历史"}]

        async def list_agents(_task_id):
            return []

        service.get_checkpoint_messages = get_checkpoint_messages
        service.list_agents = list_agents
        async def task_thread_id(_task_id):
            return "thread-a"

        service._task_thread_id = task_thread_id

        swarm_reader = service._build_swarm_reader_tools("task-a")[0]
        result = await swarm_reader.ainvoke({"agent_id": "swarm-current"})
        assert "协作历史" in result
        with pytest.raises(ValueError, match="不属于当前任务"):
            await swarm_reader.ainvoke({"agent_id": "swarm-foreign"})
        with pytest.raises(ValueError, match="不属于当前任务"):
            await swarm_reader.ainvoke({"agent_id": "missing"})

        patrol_reader = service._build_patrol_reader_tools("task-a")[1]
        with pytest.raises(ValueError, match="该小兵不属于当前任务"):
            await patrol_reader.ainvoke({"agent_id": "swarm-current"})

    asyncio.run(run())


def _checkpoint(checkpoint_id: str, messages: list) -> SimpleNamespace:
    return SimpleNamespace(
        config={"configurable": {"checkpoint_id": checkpoint_id}},
        checkpoint={"channel_values": {"messages": messages}},
    )


def test_checkpoint_preflight_selects_latest_valid_base():
    valid = _checkpoint("valid", [HumanMessage(content="旧消息")])
    invalid = _checkpoint("invalid", [
        AIMessage(content="", tool_calls=[
            {"name": "broken", "args": {}, "id": "call-broken", "type": "tool_call"}
        ]),
        HumanMessage(content="插入消息"),
    ])

    class FakeSaver:
        def __init__(self, checkpoints):
            self.checkpoints = checkpoints
            self.listed = False

        async def aget_tuple(self, _config):
            return self.checkpoints[0] if self.checkpoints else None

        async def alist(self, _config):
            self.listed = True
            for item in self.checkpoints:
                yield item

    async def run() -> None:
        saver = FakeSaver([valid])
        assert await select_checkpoint_base(saver, "thread") is None
        assert saver.listed is False

        saver = FakeSaver([invalid, valid])
        assert await select_checkpoint_base(saver, "thread") == "valid"
        assert saver.listed is True

        saver = FakeSaver([invalid])
        assert await select_checkpoint_base(saver, "thread") == EMPTY_CHECKPOINT_ID

        saver = FakeSaver([])
        assert await select_checkpoint_base(saver, "thread") is None

    asyncio.run(run())


def test_gateway_transmits_checkpoint_id_to_runnable_config(monkeypatch):
    import backend.app.gateway.services as gateway_services
    from focus.runtime.runs.limits import DEFAULT_AGENT_RECURSION_LIMIT

    captured = {}

    async def fake_run_agent(**kwargs):
        captured.update(kwargs)

    class FakeRunManager:
        def create(self, **kwargs):
            return SimpleNamespace(run_id=kwargs.get("run_id") or "run-1", task=None)

    state = SimpleNamespace(
        stream_bridge=object(), run_manager=FakeRunManager(),
        checkpointer=object(), store=object(),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=state),
        state=SimpleNamespace(current_user=None),
    )
    body = RunCreateRequest(
        input={"messages": [{"role": "human", "content": "继续"}]},
        context={"run_id": "run-1", "checkpoint_id": "valid-base"},
    )
    monkeypatch.setattr(gateway_services, "run_agent", fake_run_agent)

    async def run() -> None:
        record = await gateway_services.start_run(body, "thread-1", request)
        await record.task

    asyncio.run(run())
    assert captured["runnable_config"]["configurable"]["checkpoint_id"] == "valid-base"
    assert captured["runnable_config"]["recursion_limit"] == DEFAULT_AGENT_RECURSION_LIMIT
