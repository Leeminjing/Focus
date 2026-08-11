"""
本文件对外提供 get_context7_tools 异步函数，为承诺层隔离加载 Context7 MCP 工具。

对外提供:
    get_context7_tools(): 异步函数，返回承诺层独占的 Context7 工具列表

输入:
    url — Context7 HTTP MCP endpoint

输出:
    list[BaseTool] — resolve-library-id 与 query-docs 工具；失败时返回空列表

工作流:
    (1) 构造 HTTP 传输的 Context7 MCP server 参数。
    (2) 配置 CONTEXT7_API_KEY 时注入 Authorization: Bearer header；未配置时匿名连接。
    (3) 复用 load_mcp_tools() 正确 await client.get_tools()。

示例:
    tools = await get_context7_tools("https://mcp.context7.com/mcp")
"""

import os

from langchain_core.tools import BaseTool

from focus.mcp.tools import load_mcp_tools


async def get_context7_tools(url: str) -> list[BaseTool]:
    params: dict = {
        "transport": "http",
        "url": url,
    }
    api_key = os.getenv("CONTEXT7_API_KEY")
    if api_key:
        params["headers"] = {"Authorization": f"Bearer {api_key}"}
    return await load_mcp_tools({"context7": params})
