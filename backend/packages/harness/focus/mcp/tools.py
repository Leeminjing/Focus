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
    (4) 用 dict 构造 MultiServerMCPClient 并获取工具；client 同时登记到模块级
        _MCP_CLIENTS 注册表保活，防止其被垃圾回收后 stdio 子进程随之中断
        （langchain-mcp-adapters 0.2+ 无 context-manager，client 必须显式持有）
    (4.5) 由独立后台任务持有常驻 session，并在同一任务内进入和退出上下文：
        get_tools() 默认"每次工具调用新建 session"，会导致 playwright 等有状态
        server 的页面上下文跨调用丢失（navigate 后 snapshot 看到 about:blank）；
        绑定 session 后工具复用同一连接与浏览器实例，同时不污染 HTTP 请求任务的
        AnyIO cancel scope 栈
    (5) 单 server 连接失败 log warn 跳过，不抛异常

    close_mcp_sessions(): 在应用退出时关闭全部常驻 session

示例:
    tools = await get_mcp_tools()
    for tool in tools:
        print(tool.name)
"""

import asyncio
import logging

from langchain_core.tools import BaseTool

from focus.config.extensions_config import get_extensions_config
from focus.mcp.client import build_servers_config

logger = logging.getLogger(__name__)

# client/session 保活注册表：持有引用，防止 GC 回收导致 stdio 子进程终止。
_MCP_CLIENTS: list[object] = []
_MCP_SESSIONS: list["_PersistentMcpSession"] = []


class _PersistentMcpSession:
    """在专属 asyncio task 内完整持有一个 MCP session 上下文。"""

    def __init__(self, client, server_name: str, load_tools_from_session):
        loop = asyncio.get_running_loop()
        self._client = client
        self._server_name = server_name
        self._load_tools = load_tools_from_session
        self._ready = loop.create_future()
        self._stop = asyncio.Event()
        self._task = loop.create_task(
            self._run(), name=f"focus-mcp-session-{server_name}"
        )

    async def _run(self) -> None:
        try:
            async with self._client.session(self._server_name) as session:
                tools = await self._load_tools(session)
                if not self._ready.done():
                    self._ready.set_result(tools)
                await self._stop.wait()
        except asyncio.CancelledError:
            if not self._ready.done():
                self._ready.cancel()
            raise
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            else:
                logger.warning(
                    "MCP Server '%s' 常驻 session 异常退出",
                    self._server_name,
                    exc_info=True,
                )

    async def start(self) -> list[BaseTool]:
        """等待 session 初始化；shield 防止请求取消直接取消共享 future。"""
        return await asyncio.shield(self._ready)

    async def close(self) -> None:
        """通知 owner task 退出，使 session 在进入它的同一任务内关闭。"""
        if not self._ready.done():
            self._task.cancel()
        else:
            self._stop.set()
        await asyncio.gather(self._task, return_exceptions=True)


async def close_mcp_sessions() -> None:
    """关闭并清空当前进程持有的全部 MCP 常驻 session。"""
    sessions = list(_MCP_SESSIONS)
    _MCP_SESSIONS.clear()
    _MCP_CLIENTS.clear()
    if sessions:
        await asyncio.gather(*(session.close() for session in sessions))


async def load_mcp_tools(servers_config: dict[str, dict]) -> list[BaseTool]:
    """连接一组 MCP server；单个 server 失败时跳过，其余 server 继续加载。"""
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_mcp_adapters.tools import load_mcp_tools as load_tools_from_session
    except ImportError:
        logger.warning("langchain-mcp-adapters 未安装，MCP 工具不可用")
        return []

    tools: list[BaseTool] = []
    for server_name, params in servers_config.items():
        try:
            client = MultiServerMCPClient({server_name: params})
            owner = _PersistentMcpSession(client, server_name, load_tools_from_session)
            try:
                server_tools = await owner.start()
            except BaseException:
                await owner.close()
                raise
            _MCP_CLIENTS.append(client)
            _MCP_SESSIONS.append(owner)
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
