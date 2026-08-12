"""spawn_agent 工具测试：权限继承过滤、结果返回形状、system_prompt 透传。"""

import asyncio
import os

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage

import focus.tools.builtins.spawn_agent_tool as spawn_mod
from focus.tools.builtins.spawn_agent_tool import build_spawn_agent_tool


class FakeChildGraph:
    """最小 fake：astream 产出一轮 values，最后一条为 AI 消息。"""

    async def astream(self, input, context=None, stream_mode=None):
        yield "values", {"messages": [HumanMessage(content="任务"), AIMessage(content="子 Agent 完成结果")]}


def _runtime(permissions, model_name="deepseek-v4-flash"):
    return ToolRuntime(
        state={}, context={"workspace": "C:/ws", "permissions": permissions, "model_name": model_name},
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


def test_spawn_agent_requires_workspace_context():
    tool = build_spawn_agent_tool()
    runtime = ToolRuntime(
        state={}, context={}, config={}, stream_writer=None, tool_call_id=None, store=None, tools=[]
    )
    try:
        asyncio.run(tool.ainvoke({"task": "x", "runtime": runtime}))
    except RuntimeError as exc:
        assert "工作区上下文" in str(exc)
    else:
        raise AssertionError("缺少 workspace 上下文时必须报错")
