"""
本文件对外提供 RegisterTool 协议接口与 ToolInfo 信息载体，作为可注册工具的统一抽象。

对外提供:
    RegisterTool(Protocol) — 只要暴露 build_tool() 且返回 BaseTool 即视为一个 tool（结构类型，无需继承）
    ToolInfo(dataclass) — 工具统一对外信息载体，携带 BaseTool 与元数据（name/label/description/source）

输入:
    RegisterTool: 无输入 —— 纯接口定义，实现方需实现 build_tool()
    ToolInfo: name/label/description/source/build_tool() 与可选 prompt_snippet

输出:
    RegisterTool — 结构协议，用于判定某个对象是否为可注册工具
    ToolInfo — 供 get_available_tools() 与 equipment() 使用的统一工具信息单元

具体工作流:
    (1) RegisterTool 是一个 Protocol：任何对象只要实现 build_tool() -> BaseTool 即被视为工具，
        具体如何实现、由谁实现系统不关心（对应「实现该接口的都算 tool，具体实现系统不关心」）。
    (2) ToolInfo 是工具在装配与展示之间的一致信息载体：build_tool() 产出可装配的 BaseTool，
        source 标明来源（builtin/custom/mcp/plugin），label 供 UI 展示，description 给 LLM。

示例:
    class MyTool:
        def build_tool(self) -> BaseTool:
            return create_some_tool()
    info = ToolInfo(name="my_tool", label="My Tool", description="...", source="custom", build_tool=MyTool().build_tool)
"""

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from langchain_core.tools import BaseTool  # noqa: F401


@runtime_checkable
class RegisterTool(Protocol):
    """可注册工具的结构协议：任何暴露 build_tool() -> BaseTool 的对象均视为一个 tool。"""

    def build_tool(self) -> BaseTool:
        """构建并返回一个可装配的 BaseTool。"""
        ...


@dataclass(frozen=True)
class ToolInfo:
    """工具统一对外信息载体：BaseTool + 元数据，供装配与前端展示共用。

    字段:
        name: str — 工具名（LLM 调用用）
        label: str — 人读的 UI 展示名
        description: str — 给 LLM 的描述
        source: str — 来源标记（builtin/custom/mcp/plugin）
        build_tool: Callable[[], BaseTool] — 构建可装配 BaseTool 的闭包
        prompt_snippet: str | None — 可选，Available tools 区的一行摘要
    """

    name: str
    label: str
    description: str
    source: str
    build_tool: Callable[[], BaseTool]
    prompt_snippet: str | None = None

    def tool(self) -> BaseTool:
        """返回可装配的 BaseTool 实例。"""
        return self.build_tool()
