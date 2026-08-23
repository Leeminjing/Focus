"""
本文件对外提供 get_available_tools 异步函数，作为「获取全部可用 tool」的统一对外入口。

对外提供:
    get_available_tools — 聚合各实现 RegisterTool 的来源，返回 list[ToolInfo]（每项带 source）

输入:
    app_config: AppConfig | None  — 兼容参数（保留签名，未生效）
    tool_groups: list[str] | None  — 兼容参数（config.yaml 声明式工具已随沙箱移除，不再生效）

输出:
    list[ToolInfo] — 全部可用工具（builtin + custom + mcp + plugin；每项含 source + BaseTool）

具体工作流:
    (1) builtin: 代码级内置工具（工作区/联网），source="builtin"
    (2) custom: 经 ToolRegistry 扫描 ~/.focus/tools/<name>/（source="custom"）
    (3) mcp: 经 get_mcp_tools_cached()（source="mcp"）
    (4) plugin: 经 get_plugin_registry().tools()（source="plugin"）
    (5) 各来源包装为 ToolInfo 后合并返回；单个来源失败仅记录日志，不中断其余来源

示例:
    tools = await get_available_tools()  # -> list[ToolInfo]
    agent = create_agent(model, tools=[t.tool() for t in tools])
"""

import logging

from langchain_core.tools import BaseTool

from focus.tools.interfaces import ToolInfo

logger = logging.getLogger(__name__)


def _wrap_as_toolinfo(base_tool: BaseTool, source: str) -> ToolInfo:
    """把一个 BaseTool 包装为 ToolInfo（source 标记 + 元数据从 BaseTool 提取）。"""
    return ToolInfo(
        name=getattr(base_tool, "name", "") or "",
        label=getattr(base_tool, "name", "") or "",
        description=getattr(base_tool, "description", "") or "",
        source=source,
        build_tool=lambda t=base_tool: t,
        prompt_snippet=None,
    )


async def _builtin_tools() -> list[ToolInfo]:
    """从代码级内置工具池收集（工作区 + 联网），source="builtin"。"""
    try:
        from focus.tools.builtins.workspace_tools import WORKSPACE_TOOLS
        from focus.tools.builtins.web_tools import web_fetch, web_search

        builtin = [*WORKSPACE_TOOLS, web_search, web_fetch]
        return [_wrap_as_toolinfo(tool_, "builtin") for tool_ in builtin]
    except Exception:
        logger.error("内置工具加载失败", exc_info=True)
        return []


async def _custom_tools() -> list[ToolInfo]:
    """从 ToolRegistry 获取用户自定义工具（source="custom"）。"""
    try:
        from focus.tools.registry import get_tool_registry

        return get_tool_registry().tools()
    except Exception:
        logger.error("用户自定义工具加载失败", exc_info=True)
        return []


async def _mcp_tools() -> list[ToolInfo]:
    """从 MCP 缓存获取远端工具（source="mcp"）。"""
    try:
        from focus.mcp.cache import get_mcp_tools_cached

        return [_wrap_as_toolinfo(tool_, "mcp") for tool_ in await get_mcp_tools_cached()]
    except Exception:
        logger.error("MCP 工具加载失败", exc_info=True)
        return []


async def _plugin_tools() -> list[ToolInfo]:
    """从插件注册表获取插件提供的工具（source="plugin"）。"""
    try:
        from focus.plugins import get_plugin_registry

        return [_wrap_as_toolinfo(tool_, "plugin") for tool_ in get_plugin_registry().tools()]
    except Exception:
        logger.error("插件工具加载失败", exc_info=True)
        return []


async def get_available_tools(
    app_config=None,
    tool_groups: list[str] | None = None,
) -> list[ToolInfo]:
    """获取全部可用的 tool（builtin + custom + mcp + plugin），每项带 source。"""
    tools: list[ToolInfo] = []
    tools.extend(await _builtin_tools())
    tools.extend(await _custom_tools())
    tools.extend(await _mcp_tools())
    tools.extend(await _plugin_tools())
    return tools
