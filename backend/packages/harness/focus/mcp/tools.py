"""
本文件对外提供 get_mcp_tools 异步函数，连接已启用的 MCP Server 并获取远端工具列表，
转换为 LangChain BaseTool 列表供 lead_agent 绑定调用。

对外提供:
    get_mcp_tools(): 异步函数，返回所有已启用 MCP Server 的工具列表

本文件负责: 加载配置 → 翻译 → 连接 → 获取工具 的完整链路。

输入:
    无参数 — 函数内部自行调用 get_extensions_config() 加载 extensions_config.json

输出:
    list[BaseTool] — MCP Server 远端工具转换后的 LangChain Tool 列表

工作流:
    (1) 调用 get_extensions_config() 加载 extensions_config.json
    (2) 调用 build_servers_config() 将配置翻译为 MultiServerMCPClient 可接受的 dict
    (3) 若所有 server 均 disabled 返回 []
    (4) 用 dict 构造 MultiServerMCPClient 并获取工具
    (5) 单 server 连接失败 log warn 跳过，不抛异常

示例:
    tools = await get_mcp_tools()
    for tool in tools:
        print(tool.name)
"""

import logging

from langchain_core.tools import BaseTool

from focus.config.extensions_config import get_extensions_config
from focus.mcp.client import build_servers_config

logger = logging.getLogger(__name__)


async def load_mcp_tools(servers_config: dict[str, dict]) -> list[BaseTool]:
    """连接一组 MCP server；单个 server 失败时跳过，其余 server 继续加载。"""
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning("langchain-mcp-adapters 未安装，MCP 工具不可用")
        return []

    tools: list[BaseTool] = []
    for server_name, params in servers_config.items():
        try:
            client = MultiServerMCPClient({server_name: params})
            server_tools = await client.get_tools()
            tools.extend(server_tools)
            logger.info("MCP Server '%s' 连接成功，获取 %d 个工具", server_name, len(server_tools))
        except Exception:
            logger.warning("MCP Server '%s' 连接失败，已跳过", server_name, exc_info=True)
    return tools


async def get_mcp_tools() -> list[BaseTool]:
    try:
        extensions_config = get_extensions_config("extensions_config.json")
        servers_config = build_servers_config(extensions_config)
    except FileNotFoundError:
        logger.warning("extensions_config.json 不存在，MCP 工具不可用")
        return []
    except Exception:
        logger.error("加载 MCP 配置失败", exc_info=True)
        return []

    if not servers_config:
        return []
    return await load_mcp_tools(servers_config)
