"""
本文件对外提供 build_spawn_agent_tool 工厂函数，实现主 Agent 树形派生子 Agent 能力。

对外提供:
    build_spawn_agent_tool — 返回 spawn_agent 工具（async），供主 Agent 装配

输入:
    spawn_agent(task, system_prompt=None):
        task: str — 子任务描述（作为子 Agent 的 HumanMessage 输入）
        system_prompt: str | None — 自定义子 Agent 系统提示词，None 时使用默认模板
        runtime: ToolRuntime — LangGraph 注入，提供 workspace/permissions/model_name 上下文

输出:
    str — 子 Agent 的最终回答文本，成为主 Agent 的 ToolMessage 进入主对话

具体工作流:
    (1) 从 runtime.context 读取 workspace/permissions/model_name
    (2) 独立装配子 Agent：make_lead_agent（工具=按权限过滤的工作区工具，不含 spawn_agent 防递归）
    (3) child.astream(values) 收集最终消息，取最后一条 AI 消息文本
    (4) 子 Agent 不发布 SSE、不持久化 checkpoint，完成后即销毁，结果返回主 Agent

示例:
    from focus.tools.builtins.spawn_agent_tool import build_spawn_agent_tool
    tools = [*workspace_tools, build_spawn_agent_tool()]
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool, tool

from focus.agents.lead import make_lead_agent
from focus.runtime.runs.events import stream_text
from focus.tools.builtins.workspace_tools import select_workspace_tools

_DEFAULT_CHILD_PROMPT = (
    "你是 Focus 的辅助子 Agent。独立完成用户交给你的任务，使用工作区工具。"
    "完成后用简洁的中文汇报结果，不要描述过程。"
)


def build_spawn_agent_tool() -> BaseTool:
    """构建 spawn_agent 工具（仅装配给主 Agent，子 Agent 不再携带以限制派生深度为 1 层）。"""

    @tool
    async def spawn_agent(task: str, runtime: ToolRuntime, system_prompt: str | None = None) -> str:
        """派生一个临时子 Agent 独立完成子任务并返回其最终回答；子 Agent 完成后即销毁。"""
        context = runtime.context
        if not isinstance(context, dict) or not context.get("workspace"):
            raise RuntimeError("缺少工作区上下文: runtime.context['workspace']")
        permissions = list(context.get("permissions") or ["read"])
        model_name = context.get("model_name")

        child = await make_lead_agent(
            model_name=model_name,
            tools=select_workspace_tools(permissions),
            system_prompt=system_prompt or _DEFAULT_CHILD_PROMPT,
        )

        final_text = ""
        async for _mode, chunk in child.astream(
            {"messages": [HumanMessage(content=task)]},
            context=context,
            stream_mode=["values"],
        ):
            if not isinstance(chunk, dict):
                continue
            messages = chunk.get("messages") or []
            if not messages:
                continue
            last = messages[-1]
            if isinstance(last, AIMessage):
                final_text = stream_text(last.content)
        return final_text or "（子 Agent 未产生回答）"

    return spawn_agent
