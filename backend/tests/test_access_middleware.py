"""唯一准入点的判定、批准载荷与批准生命周期用例。

输入为工具调用请求、受治理上下文与人工决定；输出为判定结论、中断载荷与工具结果。
工作流先逐类锁定判定（无本地效果 / 委托执行 / 可结构化枚举 / 不透明），再锁定载荷字段，
最后用一个带检查点的最小图验证「中断先于副作用」与「获批只作用于当前这一次调用」。
"""

import asyncio
from pathlib import Path
from typing import Any, TypedDict

import pytest
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

import focus.security.middleware as middleware_module
from focus.security.approval import ApprovalRequest
from focus.security.effects import (
    DELEGATED_EXECUTION_EFFECT,
    NO_LOCAL_EFFECT,
    ResolvedFsEffect,
    declare_effect,
    structured_fs,
)
from focus.security.middleware import AccessPolicyMiddleware, _admit
from focus.tools.builtins.workspace_tools import read_file

WORKSPACE = "C:\\ws"


@tool
def plain_path_tool(path: str) -> str:
    """Structured tool that declares nothing."""
    return path


@tool
def declared_free(text: str) -> str:
    """Tool declared as having no local effect."""
    return text


@tool
def declared_delegated(task: str) -> str:
    """Tool declared as creating a delegated execution."""
    return task


declare_effect(declared_free, NO_LOCAL_EFFECT)
declare_effect(declared_delegated, DELEGATED_EXECUTION_EFFECT)


def _structured_tool(resolver) -> Any:
    """按解析器现造一个可结构化枚举的工具，用于覆盖解析结果形状。"""
    return declare_effect(plain_path_tool.model_copy(deep=True), structured_fs(resolver))


def _runtime(context: dict[str, Any]) -> ToolRuntime:
    return ToolRuntime(
        state={}, context=context, config={}, stream_writer=None,
        tool_call_id=None, store=None, tools=[],
    )


def _request(tool_obj, args: dict[str, Any], context: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": getattr(tool_obj, "name", "unknown"), "args": args, "id": "call-1"},
        tool=tool_obj,
        state={},
        runtime=_runtime(context),
    )


def _context(tmp_path, **extra) -> dict[str, Any]:
    return {"workspace": str(tmp_path), **extra}


def test_no_local_effect_is_allowed_without_asking(tmp_path):
    admission = _admit(_request(declared_free, {"text": "x"}, _context(tmp_path)))
    assert admission.asked is False


def test_delegated_execution_is_allowed_without_asking(tmp_path):
    admission = _admit(_request(declared_delegated, {"task": "t"}, _context(tmp_path)))
    assert admission.asked is False


def test_undeclared_tool_is_treated_as_opaque_and_asks(tmp_path):
    admission = _admit(_request(plain_path_tool, {"path": "a"}, _context(tmp_path)))
    assert admission.asked is True


def test_read_file_inside_root_is_allowed_and_args_are_canonical(tmp_path):
    admission = _admit(
        _request(read_file, {"path": "a.txt"}, _context(tmp_path, agent_role="main"))
    )
    assert admission.asked is False
    assert admission.args["path"] == str((tmp_path / "a.txt").resolve())


def test_read_file_outside_root_asks_with_full_payload(tmp_path):
    request = _request(
        read_file, {"path": "../outside.txt"}, _context(tmp_path, agent_role="main")
    )
    admission = _admit(request)
    assert admission.asked is True
    payload = admission.request.payload()
    assert payload["type"] == "access_review"
    assert payload["tool"] == "read_file"
    assert payload["access_mode"] == "workspace"
    assert payload["cwd"] == str(tmp_path.resolve())
    assert payload["agent_role"] == "main"
    assert payload["reads"] == [str((tmp_path.parent / "outside.txt").resolve())]
    assert payload["writes"] == []


def test_structured_target_outside_root_is_still_canonical_for_execution(tmp_path):
    admission = _admit(_request(read_file, {"path": "../outside.txt"}, _context(tmp_path)))
    assert admission.args["path"] == str((tmp_path.parent / "outside.txt").resolve())


def test_full_mode_allows_outside_structured_target(tmp_path):
    admission = _admit(
        _request(read_file, {"path": "../outside.txt"}, _context(tmp_path, access_mode="full"))
    )
    assert admission.asked is False


def test_shell_asks_in_workspace_mode_and_carries_command(tmp_path):
    from focus.tools.builtins.workspace_tools import powershell

    admission = _admit(
        _request(powershell, {"command": "Remove-Item ../x"}, _context(tmp_path, agent_role="worker"))
    )
    assert admission.asked is True
    payload = admission.request.payload()
    assert payload["command"] == "Remove-Item ../x"
    assert payload["agent_role"] == "worker"
    assert payload["reads"] == []
    assert payload["writes"] == []


def test_read_and_write_targets_are_labelled_separately(tmp_path):
    """同一路径可能被读、被写或两者兼有：载荷分开承载，人据此判断会不会被改写。"""
    target = tmp_path.parent / "outside.txt"
    tool = _structured_tool(lambda args, context: ResolvedFsEffect(reads=(target,), writes=(target,)))

    admission = _admit(_request(tool, {"path": "x"}, _context(tmp_path)))

    assert admission.asked is True
    payload = admission.request.payload()
    assert payload["reads"] == [str(target.resolve())]
    assert payload["writes"] == [str(target.resolve())]
    assert ApprovalRequest.from_payload(payload).targets == (str(target.resolve()),)


def test_shell_is_allowed_in_full_mode(tmp_path):
    from focus.tools.builtins.workspace_tools import powershell

    admission = _admit(
        _request(powershell, {"command": "ls"}, _context(tmp_path, access_mode="full"))
    )
    assert admission.asked is False


def test_network_tool_never_asks_even_in_workspace_mode(tmp_path):
    from focus.tools.builtins.web_tools import web_search

    admission = _admit(_request(web_search, {"query": "x"}, _context(tmp_path)))
    assert admission.asked is False


def test_unregistered_tool_is_fail_closed(tmp_path):
    admission = _admit(_request(None, {"anything": 1}, _context(tmp_path)))
    assert admission.asked is True


def test_module_holds_no_authorization_state():
    """获批只来自中断恢复值，因此模块内不存在任何可用作授权表的可变容器。"""
    tables = [
        name
        for name, value in vars(middleware_module).items()
        if not name.startswith("__") and isinstance(value, (dict, set, list))
    ]
    assert tables == []


class _GraphState(TypedDict):
    workspace: str
    path: str
    result: str
    status: str


def _approval_graph(middleware: AccessPolicyMiddleware, side_effects: list[Any]):
    async def handler(request: ToolCallRequest) -> ToolMessage:
        side_effects.append(request.tool_call["args"])
        return ToolMessage(content="executed", tool_call_id=request.tool_call["id"])

    async def node(state: _GraphState) -> dict[str, Any]:
        request = ToolCallRequest(
            tool_call={"name": "read_file", "args": {"path": state["path"]}, "id": "call-1"},
            tool=_read_file_tool(),
            state={},
            runtime=_runtime({"workspace": state["workspace"], "agent_role": "main"}),
        )
        result = await middleware.awrap_tool_call(request, handler)
        return {"result": getattr(result, "content", None), "status": getattr(result, "status", None)}

    builder = StateGraph(_GraphState)
    builder.add_node("admit", node)
    builder.add_edge(START, "admit")
    builder.add_edge("admit", END)
    return builder.compile(checkpointer=InMemorySaver()), node


def _read_file_tool():
    from focus.tools.builtins.workspace_tools import read_file

    return read_file


class _WriteState(TypedDict):
    workspace: str
    path: str
    content: str
    result: str
    status: str


def _write_graph(middleware: AccessPolicyMiddleware, side_effects: list[Any]):
    """按 (path, content) 发起一次 write_file 调用，用于覆盖「目标相同、参数不同」。"""
    from focus.tools.builtins.workspace_tools import write_file

    async def handler(request: ToolCallRequest) -> ToolMessage:
        side_effects.append(request.tool_call["args"])
        return ToolMessage(content="executed", tool_call_id=request.tool_call["id"])

    async def node(state: _WriteState) -> dict[str, Any]:
        request = ToolCallRequest(
            tool_call={
                "name": "write_file",
                "args": {"path": state["path"], "content": state["content"]},
                "id": "call-w1",
            },
            tool=write_file,
            state={},
            runtime=_runtime(
                {"workspace": state["workspace"], "permissions": ["read", "write"], "agent_role": "main"}
            ),
        )
        result = await middleware.awrap_tool_call(request, handler)
        return {"result": getattr(result, "content", None), "status": getattr(result, "status", None)}

    builder = StateGraph(_WriteState)
    builder.add_node("admit", node)
    builder.add_edge(START, "admit")
    builder.add_edge("admit", END)
    return builder.compile(checkpointer=InMemorySaver())


def test_same_target_with_different_args_asks_again(tmp_path):
    """同一工具、同一目标但参数内容不同：放行只覆盖被批准的那一次调用。"""
    side_effects: list[Any] = []
    graph = _write_graph(AccessPolicyMiddleware(), side_effects)
    outside = str(tmp_path.parent / "outside.txt")
    config = {"configurable": {"thread_id": "same-target-1"}}

    asyncio.run(graph.ainvoke({"workspace": str(tmp_path), "path": outside, "content": "a"}, config))
    assert side_effects == []
    assert _pending_payload(graph, config) is not None

    asyncio.run(graph.ainvoke(Command(resume={"decision": "approve"}), config))
    assert [item["content"] for item in side_effects] == ["a"]

    side_effects.clear()
    asyncio.run(graph.ainvoke({"workspace": str(tmp_path), "path": outside, "content": "b"}, config))
    assert side_effects == [], "参数不同的同目标调用不得沿用上次放行"
    assert _pending_payload(graph, config) is not None


def _graph_state(graph, config):
    return graph.get_state(config)


def _pending_payload(graph, config):
    state = _graph_state(graph, config)
    for task in state.tasks:
        for item in getattr(task, "interrupts", ()) or ():
            return item.value
    return None


def test_interrupt_fires_before_any_side_effect_and_grants_once(tmp_path):
    side_effects: list[Any] = []
    middleware = AccessPolicyMiddleware()
    graph, _ = _approval_graph(middleware, side_effects)
    outside = str(tmp_path.parent / "outside.txt")

    first = {"configurable": {"thread_id": "approve-1"}}
    asyncio.run(graph.ainvoke({"workspace": str(tmp_path), "path": outside}, first))

    payload = _pending_payload(graph, first)
    assert payload is not None
    assert payload["tool"] == "read_file"
    assert side_effects == []

    asyncio.run(graph.ainvoke(Command(resume={"decision": "approve"}), first))
    assert len(side_effects) == 1
    assert side_effects[0]["path"] == str(Path(outside).resolve())

    side_effects.clear()
    second = {"configurable": {"thread_id": "approve-2"}}
    asyncio.run(graph.ainvoke({"workspace": str(tmp_path), "path": outside}, second))
    assert len(side_effects) == 0
    assert _pending_payload(graph, second) is not None


def test_rejection_skips_execution_and_returns_visible_error(tmp_path):
    side_effects: list[Any] = []
    middleware = AccessPolicyMiddleware()
    graph, _ = _approval_graph(middleware, side_effects)
    config = {"configurable": {"thread_id": "reject-1"}}

    asyncio.run(
        graph.ainvoke({"workspace": str(tmp_path), "path": str(tmp_path.parent / "outside.txt")}, config)
    )
    assert side_effects == []

    result = asyncio.run(graph.ainvoke(Command(resume={"decision": "reject"}), config))
    assert side_effects == []
    assert result["status"] == "error"
    assert "未批准" in (result["result"] or "")


def test_in_root_structured_call_does_not_interrupt(tmp_path):
    side_effects: list[Any] = []
    middleware = AccessPolicyMiddleware()
    graph, _ = _approval_graph(middleware, side_effects)
    config = {"configurable": {"thread_id": "in-root-1"}}

    result = asyncio.run(graph.ainvoke({"workspace": str(tmp_path), "path": "inside.txt"}, config))
    assert _pending_payload(graph, config) is None
    assert len(side_effects) == 1
    assert result["result"] == "executed"


def _agent_source() -> str:
    path = Path(__file__).parents[1] / "packages" / "harness" / "focus" / "agents" / "lead" / "agent.py"
    return path.read_text(encoding="utf-8")


def test_access_policy_is_installed_unconditionally_at_the_front():
    """准入门由 make_lead_agent 无条件装配于链首。

    它以函数体缩进装配（不嵌在任何条件分支内），位于 desktop 注入的角色中间件之外，
    因此 main / teammate / worker / patrol 与内联子执行统一具备准入层；又因位于链首而
    取得最外层，先于任何「可恢复工具错误闭合」触发中断。
    """
    source = _agent_source()
    prepend = "\n    middlewares = [AccessPolicyMiddleware(), *middlewares]"
    assert prepend in source, "准入门必须以函数体缩进无条件装配"
    assert source.index(prepend) > source.index("PluginBridgeMiddleware(get_plugin_registry())")


def test_worker_streams_interrupt_snapshots_regardless_of_commitment():
    """中断可观测性不得依赖承诺层开关：准入门与承诺层无关。"""
    path = Path(__file__).parents[1] / "packages" / "harness" / "focus" / "runtime" / "runs" / "worker.py"
    source = path.read_text(encoding="utf-8")
    forcing = 'stream_modes_list = list(dict.fromkeys([*requested_stream_modes, "messages", "values", "custom"]))'
    assert forcing in source
    assert "if app_config.commitment.enabled:" not in source.split(forcing)[0][-400:]


def test_workspace_tools_hold_no_admission_call():
    """准入判定只在唯一准入点：工具体内不得出现任何判定调用。"""
    path = (
        Path(__file__).parents[1]
        / "packages" / "harness" / "focus" / "tools" / "builtins" / "workspace_tools.py"
    )
    source = path.read_text(encoding="utf-8")
    for forbidden in ("decide_path_access", "AccessDecision", "不属于当前工作区"):
        assert forbidden not in source
