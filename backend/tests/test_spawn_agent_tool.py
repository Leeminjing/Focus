"""spawn_agent 工具测试：权限继承过滤、结果返回形状、system_prompt 透传、单调派生、子执行隔离与父图配对。

输入为父级受治理安全上下文与工具参数，以及在真实父图（模型节点 + ToolNode + 父 thread checkpoint）中一次发出
两个 spawn_agent 调用的派发；输出为子 Agent 的装配参数、子图上下文、子执行运行配置、最终文本，以及父 Context
在派发后的消息序列与 tool_calls/工具结果配对计数。
工作流先锁定权限继承过滤与 system_prompt 透传，再锁定子图上下文由父级单调派生且不放大，
随后锁定子执行携带自己的执行身份（独立 thread/namespace），最后断言缺失安全上下文即失败、
以及子执行失败收口为工具结果文本；父图用例另断言派发后父 Context 只新增工具结果、
子任务文本不进入父消息通道、且每条 tool_calls 都有配对的工具结果（成功与失败各一次）。
"""

import asyncio
from pathlib import Path

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

import focus.tools.builtins.spawn_agent_tool as spawn_mod
from focus.security.context import (
    AuthorizationIdentity,
    ExecutionProfile,
    RoutingIdentity,
    derive_security_context,
    security_context_of,
)
from focus.security.policy import AccessMode, workspace_roots
from focus.tools.builtins.spawn_agent_tool import build_spawn_agent_tool


class FakeChildGraph:
    """最小 fake：astream 产出一轮 values，最后一条为 AI 消息，并记录收到的输入、运行配置与子图上下文。"""

    def __init__(self) -> None:
        self.context = None
        self.config = None
        self.input = None

    async def astream(self, input, context=None, stream_mode=None, config=None):
        self.context = context
        self.config = config
        self.input = input
        yield "values", {"messages": [HumanMessage(content="任务"), AIMessage(content="子 Agent 完成结果")]}


class FailingChildGraph:
    """最小 fake：子执行在流式过程中失败，用于验证失败收口。"""

    async def astream(self, input, context=None, stream_mode=None, config=None):
        raise RuntimeError("子 Agent 模型请求被拒")
        yield  # pragma: no cover


def _governed_context(permissions, model_name="deepseek-v4-flash") -> dict:
    workspace = Path.cwd()
    return derive_security_context(
        ExecutionProfile(
            authorization=AuthorizationIdentity(
                workspace=workspace,
                roots=workspace_roots(workspace),
                permissions=tuple(permissions),
                access_mode=AccessMode.WORKSPACE,
                agent_role="main",
            ),
            routing=RoutingIdentity("thread-1", "ws-1", "main:task-1", "task-1", ""),
            model_name=model_name,
        )
    ).to_runtime_context()


def _runtime(permissions, model_name="deepseek-v4-flash"):
    return ToolRuntime(
        state={}, context=_governed_context(permissions, model_name),
        config={}, stream_writer=None, tool_call_id=None, store=None, tools=[],
    )


def test_spawn_agent_inherits_permissions_and_returns_final_text():
    captured = {}

    async def fake_make_lead_agent(**kwargs):
        captured.update(kwargs)
        return FakeChildGraph()

    original = spawn_mod.make_lead_agent
    spawn_mod.make_lead_agent = fake_make_lead_agent
    try:
        tool = build_spawn_agent_tool()
        result = asyncio.run(tool.ainvoke(
            {"task": "审查代码风格", "runtime": _runtime(["read"])}
        ))
        assert result == "子 Agent 完成结果"
        # 权限继承：read 只装配读工具，无 write/shell
        names = {t.name for t in captured["tools"]}
        assert "read_file" in names and "list_files" in names
        assert "write_file" not in names and "powershell" not in names
        # 子 Agent 不携带 spawn_agent（防递归）
        assert "spawn_agent" not in names
        # 默认 system_prompt 注入
        assert "辅助子 Agent" in captured["system_prompt"]
    finally:
        spawn_mod.make_lead_agent = original


def test_spawn_agent_custom_system_prompt_and_model():
    captured = {}

    async def fake_make_lead_agent(**kwargs):
        captured.update(kwargs)
        return FakeChildGraph()

    original = spawn_mod.make_lead_agent
    spawn_mod.make_lead_agent = fake_make_lead_agent
    try:
        tool = build_spawn_agent_tool()
        asyncio.run(tool.ainvoke({
            "task": "统计行数",
            "system_prompt": "你是专用统计 Agent",
            "runtime": _runtime(["read", "write"], model_name="custom-model"),
        }))
        assert captured["system_prompt"] == "你是专用统计 Agent"
        assert captured["model_name"] == "custom-model"
    finally:
        spawn_mod.make_lead_agent = original


def test_spawn_agent_child_runs_with_isolated_execution_identity():
    """子执行携带自己的执行身份：显式声明独立的 thread 与 namespace，不继承父 Context 的会话身份。"""

    captured = {}

    async def fake_make_lead_agent(**kwargs):
        graph = FakeChildGraph()
        captured["graph"] = graph
        return graph

    original = spawn_mod.make_lead_agent
    spawn_mod.make_lead_agent = fake_make_lead_agent
    try:
        tool = build_spawn_agent_tool()
        result = asyncio.run(tool.ainvoke({"task": "独立子任务", "runtime": _runtime(["read"])}))
    finally:
        spawn_mod.make_lead_agent = original

    graph = captured["graph"]
    configurable = (graph.config or {}).get("configurable") or {}
    assert configurable.get("thread_id"), "子执行必须显式声明自己的 thread，不能继承父运行配置"
    assert configurable.get("checkpoint_ns"), "子执行必须显式声明自己的 namespace"
    assert "thread-1" not in str(configurable.get("thread_id")), "子执行不与父运行共用会话身份"
    assert graph.input == {"messages": [HumanMessage(content="独立子任务")]}, "子任务只作为子图输入传入"
    assert isinstance(result, str), "子执行结果必须以文本形式回填为父 Context 的工具结果"


def test_spawn_agent_closes_child_failure_into_a_tool_result():
    """子执行失败收口：不抛出，返回引用失败原因的文本，使父 Context 的 tool_calls 得到工具结果。"""

    async def fake_make_lead_agent(**kwargs):
        return FailingChildGraph()

    original = spawn_mod.make_lead_agent
    spawn_mod.make_lead_agent = fake_make_lead_agent
    try:
        tool = build_spawn_agent_tool()
        result = asyncio.run(tool.ainvoke({"task": "会失败的子任务", "runtime": _runtime(["read"])}))
    finally:
        spawn_mod.make_lead_agent = original

    assert isinstance(result, str)
    assert "子 Agent 执行失败" in result
    assert "子 Agent 模型请求被拒" in result


def test_spawn_agent_requires_governed_security_context():
    tool = build_spawn_agent_tool()
    runtime = ToolRuntime(
        state={}, context={}, config={}, stream_writer=None, tool_call_id=None, store=None, tools=[]
    )
    with pytest.raises(RuntimeError, match="受治理安全上下文"):
        asyncio.run(tool.ainvoke({"task": "x", "runtime": runtime}))


def test_spawn_agent_child_context_is_derived_and_not_wider():
    """子图上下文由父级单调派生：不是父级字典本身，权限与访问模式都不放大。"""
    captured = {}

    async def fake_make_lead_agent(**kwargs):
        captured.update(kwargs)
        graph = FakeChildGraph()
        captured["graph"] = graph
        return graph

    original = spawn_mod.make_lead_agent
    spawn_mod.make_lead_agent = fake_make_lead_agent
    try:
        tool = build_spawn_agent_tool()
        parent_runtime = _runtime(["read", "write"])
        asyncio.run(tool.ainvoke({"task": "t", "runtime": parent_runtime}))
    finally:
        spawn_mod.make_lead_agent = original

    parent = security_context_of(parent_runtime.context)
    child_context = captured["graph"].context
    assert child_context is not parent_runtime.context
    child = security_context_of(child_context)
    assert child.authorization.agent_role == "spawn_agent"
    assert set(child.authorization.permissions) <= set(parent.authorization.permissions)
    assert child.authorization.access_mode is parent.authorization.access_mode
    assert child_context["permissions"] == list(parent.authorization.permissions)


PARENT_THREAD = "parent-thread-pairing"


def _parent_graph(tool):
    """真实父图：模型节点先发出两个 spawn_agent 调用，ToolNode 收口后模型给出最终回答。"""

    async def model(state):
        emitted = [message for message in state["messages"] if isinstance(message, AIMessage) and message.tool_calls]
        if emitted:
            return {"messages": [AIMessage(content="已汇总两个子 Agent 的结果")]}
        tool_calls = [
            {"name": "spawn_agent", "args": {"task": task}, "id": f"call-{index}", "type": "tool_call"}
            for index, task in enumerate(("子任务一", "子任务二"), start=1)
        ]
        return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}

    builder = StateGraph(MessagesState)
    builder.add_node("model", model)
    builder.add_node("tools", ToolNode([tool]))
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", lambda state: "tools" if state["messages"][-1].tool_calls else END)
    builder.add_edge("tools", "model")
    return builder.compile(checkpointer=MemorySaver())


def _dispatch_through_parent(tool, children) -> list:
    async def drive() -> list:
        graph = _parent_graph(tool)
        config = {"configurable": {"thread_id": PARENT_THREAD}}
        await graph.ainvoke(
            {"messages": [HumanMessage(content="请并行校验两个方向")]},
            config=config,
            context=_governed_context(["read"]),
        )
        snapshot = await graph.aget_state(config)
        return list(snapshot.values["messages"])

    return asyncio.run(drive())


def _assert_parent_pairing(messages: list) -> list:
    calls = [message for message in messages if isinstance(message, AIMessage)]
    results = [message for message in messages if isinstance(message, ToolMessage)]
    assert sum(len(message.tool_calls) for message in calls) == len(results), "父 Context 不得留下未配对的 tool_calls"
    assert {message.tool_call_id for message in results} == {"call-1", "call-2"}, "每个 tool_call 必须各有一条工具结果"
    humans = [message for message in messages if isinstance(message, HumanMessage)]
    assert [message.content for message in humans] == ["请并行校验两个方向"], "派发不得新增或改写父 Context 的人类消息"
    assert all(message.content not in {"子任务一", "子任务二", "任务"} for message in messages), "子任务文本与子执行中间状态不得进入父消息通道"
    return results


def test_parent_graph_pairs_tool_calls_when_children_succeed():
    """父图派发成功后：父 checkpoint 里每个 spawn_agent 调用都有工具结果，子执行内容不落进父通道。"""

    children = []

    async def fake_make_lead_agent(**kwargs):
        graph = FakeChildGraph()
        children.append(graph)
        return graph

    original = spawn_mod.make_lead_agent
    spawn_mod.make_lead_agent = fake_make_lead_agent
    try:
        tool = build_spawn_agent_tool()
        messages = _dispatch_through_parent(tool, children)
    finally:
        spawn_mod.make_lead_agent = original

    results = _assert_parent_pairing(messages)
    assert [message.content for message in results] == ["子 Agent 完成结果", "子 Agent 完成结果"]
    assert len(children) == 2, "两次 spawn_agent 调用必须各自装配一个子 Agent"
    for graph in children:
        configurable = (graph.config or {}).get("configurable") or {}
        assert configurable.get("thread_id") != PARENT_THREAD, "子执行不得写入父 thread"


def test_parent_graph_pairs_tool_calls_when_children_fail():
    """父图派发失败时：父 checkpoint 仍为每个调用留下工具结果，父图不会被协议错误卡死。"""

    async def fake_make_lead_agent(**kwargs):
        return FailingChildGraph()

    original = spawn_mod.make_lead_agent
    spawn_mod.make_lead_agent = fake_make_lead_agent
    try:
        tool = build_spawn_agent_tool()
        messages = _dispatch_through_parent(tool, [])
    finally:
        spawn_mod.make_lead_agent = original

    results = _assert_parent_pairing(messages)
    assert all("子 Agent 执行失败" in message.content for message in results), "失败必须由工具结果携带失败原因收口"
    assert isinstance(messages[-1], AIMessage), "工具结果收口后父图必须能继续完成本轮模型调用"
