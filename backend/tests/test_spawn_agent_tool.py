"""spawn_agent 工具测试：权限继承过滤、结果返回形状、system_prompt 透传与单调派生。

输入为父级受治理安全上下文与工具参数；输出为子 Agent 的装配参数、子图上下文与最终文本。
工作流先锁定权限继承过滤与 system_prompt 透传，再锁定子图上下文由父级单调派生且不放大，
最后断言缺失安全上下文即失败。
"""

import asyncio
from pathlib import Path

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage

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
    """最小 fake：astream 产出一轮 values，最后一条为 AI 消息，并记录收到的子图上下文。"""

    def __init__(self) -> None:
        self.context = None

    async def astream(self, input, context=None, stream_mode=None):
        self.context = context
        yield "values", {"messages": [HumanMessage(content="任务"), AIMessage(content="子 Agent 完成结果")]}


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
