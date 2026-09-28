"""本文件对外提供运行中会话模式刷新及调用快照隔离测试。

输入为服务端异步模式读取器、受治理上下文和连续的模型或工具请求。
输出为新请求读取新模式、先前绑定仍保留原模式和模型说明随之更新的证据。
具体工作流为改变持久模式读取器的返回值，分别执行两次模型/工具中间件调用并比较结果。
示例：运行 python -m pytest backend/tests/test_live_sandbox_mode.py。
"""

import asyncio
from dataclasses import replace

from langchain.agents.middleware import ModelRequest
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import SystemMessage, ToolMessage

from backend.tests.runtime_context_support import runtime_context
from focus.security.context import security_context_of
from focus.security.execution import bind_call_execution
from focus.security.middleware import AccessPolicyMiddleware
from focus.security.model_context import FileModeContextMiddleware
from focus.security.policy import AccessMode
from focus.tools.builtins.workspace_tools import powershell


def _runtime(workspace, mode_state):
    context = runtime_context(
        agent_id="agent-1", task_id="task-1", workspace=str(workspace),
        permissions=("read", "host_command"), access_mode=AccessMode.WORKSPACE_WRITE,
    )

    async def resolve():
        return mode_state["mode"]

    security = security_context_of(context)
    context["security_context"] = replace(
        security, extras={**security.extras, "session_mode_resolver": resolve},
    )
    return ToolRuntime(
        state={}, context=context, config={}, stream_writer=None,
        tool_call_id="call-1", store=None, tools=[],
    )


def test_new_tool_call_reads_new_mode_without_mutating_prior_binding(tmp_path):
    state = {"mode": AccessMode.WORKSPACE_WRITE}
    runtime = _runtime(tmp_path, state)
    seen = []

    async def handler(request):
        seen.append(bind_call_execution(request.runtime.context, request.tool_call["id"]))
        return ToolMessage(content="executed", tool_call_id=request.tool_call["id"])

    async def call(call_id):
        request = ToolCallRequest(
            tool_call={"name": "powershell", "args": {"command": "echo ready"}, "id": call_id},
            tool=powershell, state={}, runtime=runtime,
        )
        return await AccessPolicyMiddleware().awrap_tool_call(request, handler)

    asyncio.run(call("call-1"))
    state["mode"] = AccessMode.READ_ONLY
    asyncio.run(call("call-2"))
    assert [binding.mode for binding in seen] == [AccessMode.WORKSPACE_WRITE, AccessMode.READ_ONLY]
    assert seen[0].mode is AccessMode.WORKSPACE_WRITE
    assert all(binding.mode_source == "persistent-session" for binding in seen)


def test_next_model_request_shows_persisted_mode_and_workspace(tmp_path):
    state = {"mode": AccessMode.WORKSPACE_WRITE}
    runtime = _runtime(tmp_path, state)

    async def model_call():
        request = ModelRequest(
            model=None, messages=[], system_message=SystemMessage(content="base"), runtime=runtime,
        )

        async def handler(active):
            return active.system_message.content

        return await FileModeContextMiddleware().awrap_model_call(request, handler)

    first = asyncio.run(model_call())
    state["mode"] = AccessMode.READ_ONLY
    second = asyncio.run(model_call())
    assert "workspace-write" in first
    assert "read-only" in second
    assert str(tmp_path) in first and str(tmp_path) in second
    assert "workspace-write" not in second.split("当前文件模式：", 1)[1].split("；", 1)[0]
