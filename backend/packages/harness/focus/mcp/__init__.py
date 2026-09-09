"""
lead_agent MCP 模块。

对外提供:
    client.py — 纯翻译层，McpServerConfig → MultiServerMCPClient 可消费 dict
    tools.py — 连接 MCP Server 获取远端工具列表
    cache.py — 基于 mtime 的工具缓存与热更新
"""

from focus.mcp.cache import get_mcp_tools_cached
from focus.mcp.client import build_server_params, build_servers_config
from focus.mcp.context7 import get_context7_tools
from focus.mcp.tools import close_mcp_sessions, get_mcp_tools, load_mcp_tools

__all__ = [
    "build_server_params",
    "build_servers_config",
    "close_mcp_sessions",
    "get_mcp_tools",
    "get_mcp_tools_cached",
    "get_context7_tools",
    "load_mcp_tools",
]
