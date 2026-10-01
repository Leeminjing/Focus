"""本文件对外提供运行中会话模式刷新及调用快照隔离测试。

输入为服务端异步模式读取器、受治理上下文和连续的模型或工具请求。
输出为新请求读取新模式、先前绑定仍保留原模式和模型说明随之更新的证据。
具体工作流为改变持久模式读取器的返回值，分别执行两次模型/工具中间件调用并比较结果。
示例：运行 python -m pytest backend/tests/test_live_sandbox_mode.py。
"""

import asyncio
import json
from dataclasses import replace

from langchain.agents.middleware import ModelRequest
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import SystemMessage, ToolMessage

from backend.tests.runtime_context_support import runtime_context
from focus.security.context import security_context_of
from focus.security.execution import bind_call_execution
from focus.security.middleware import AccessPolicyMiddleware
from focus.context.middleware import WorldStateMiddleware
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

    middleware = WorldStateMiddleware([], "base", frozenset(), "fixture", "v1")
    initial = asyncio.run(middleware.abefore_model({"messages": []}, runtime))
    first = "\n".join(message.content for message in initial["messages"])
    state["mode"] = AccessMode.READ_ONLY
    updated = asyncio.run(middleware.abefore_model(initial, runtime))
    second = "\n".join(message.content for message in updated["messages"])
    assert "workspace-write" in first
    assert "read-only" in second
    environment = next(message for message in initial["messages"] if 'section="environment"' in message.content)
    assert json.loads(environment.content.split("\n")[1])["workspace"] == str(tmp_path)
    permissions = next(message for message in updated["messages"] if 'section="permissions"' in message.content)
    assert 'update="replacement"' in permissions.content
    assert json.loads(permissions.content.split("\n")[1])["access_mode"] == "read-only"
