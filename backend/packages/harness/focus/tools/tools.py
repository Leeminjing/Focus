"""
本文件对外提供 get_available_tools 异步函数，作为工具汇集的唯一对外入口。

输入:
    app_config: AppConfig | None  — 兼容参数（保留签名）
    tool_groups: list[str] | None  — 兼容参数（config.yaml 声明式工具已随沙箱移除，不再生效）

输出:
    list[BaseTool] — 可用的 LangChain Tool 列表（MCP 远端工具池；工作区内置工具由
                     agent_factory 按权限注入，不在此池）

具体工作流:
    (1) 加载 MCP 远端工具（fail-soft，失败返回空列表）
    (2) 返回工具列表

示例:
    tools = await get_available_tools()
    agent = create_agent(model, tools=tools)
"""

import logging

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


async def get_available_tools(
    app_config=None,
    tool_groups: list[str] | None = None,
) -> list[BaseTool]:
    try:
        from focus.mcp.cache import get_mcp_tools_cached

        return await get_mcp_tools_cached()
    except Exception:
        logger.error("MCP 工具加载失败", exc_info=True)
        return []
