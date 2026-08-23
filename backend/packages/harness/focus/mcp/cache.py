"""
本文件提供 MCP 工具缓存机制，以 extensions_config.json 的文件修改时间 (mtime) 为刷新依据，
避免每次调用都重新连接所有 MCP Server。

对外提供:
    get_mcp_tools_cached(): 异步函数，返回缓存的 MCP 工具列表，mtime 变更时自动刷新

输入:
    无参数 — 函数内部通过 get_mcp_tools() 和 os.path.getmtime 自行获取所需数据

输出:
    list[BaseTool] — 缓存的 MCP 工具列表

工作流:
    (1) 读取 extensions_config.json 的当前 mtime
    (2) 首次调用: 记录 mtime → 调用 get_mcp_tools() → 存入缓存并返回
    (3) mtime 未变: 直接返回缓存的工具列表
    (4) mtime 变更: 重新调用 get_mcp_tools() → 更新缓存和 mtime
       MultiServerMCPClient 使用 ephemeral session 模式，旧连接随 client 对象回收自动关闭
       刷新失败时保留上次成功缓存的工具列表作为降级返回，不更新 mtime

示例:
    tools = await get_mcp_tools_cached()
    for tool in tools:
        print(tool.name)
"""

import logging
import os

from langchain_core.tools import BaseTool

from focus.mcp.tools import get_mcp_tools

logger = logging.getLogger(__name__)

_CONFIG_NAME = "extensions_config.json"

_mcp_tools: list[BaseTool] | None = None
_mtime: float | None = None


async def get_mcp_tools_cached() -> list[BaseTool]:
    global _mcp_tools, _mtime

    from focus.config.layered import layered_mtime

    current_mtime = layered_mtime(_CONFIG_NAME, _CONFIG_NAME)
    if current_mtime is None:
        logger.warning("extensions_config.json 不存在，无法检查 mtime")

    if _mcp_tools is not None and _mtime is not None and current_mtime is not None:
        if current_mtime <= _mtime:
            return _mcp_tools
        logger.info("extensions_config.json mtime 变更 (%.3f → %.3f)，刷新 MCP 工具缓存", _mtime, current_mtime)

    previous_tools = _mcp_tools

    try:
        tools = await get_mcp_tools()
    except Exception:
        logger.error("刷新 MCP 工具缓存失败", exc_info=True)
        if previous_tools is not None:
            logger.warning("降级使用上一次缓存的 %d 个工具", len(previous_tools))
            return previous_tools
        _mtime = None
        _mcp_tools = None
        return []

    _mcp_tools = tools
    _mtime = current_mtime or 0
    return tools
