"""MCP 工具的配置、连接与信任摄取边界。

get_mcp_tools(workspace=..., config=...) 接受工作区或已经冻结解析的配置。
load_mcp_tools 按 server 名与连接参数复用当前事件循环的常驻 session；
每个 session 在独立 owner task 内进入和退出，避免污染请求任务的 AnyIO 栈。
工具副本按当前配置签发信任，防止工作区覆盖修改其他 Run 的工具契约。

McpTools 保留部分成功列表，并通过 failed_servers / healthy 向缓存报告可重试状态。
失败连接在本次调用内释放；成功 session 保留给已开始的 Run，退出时由
close_mcp_sessions 统一关闭。
"""

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from langchain_core.tools import BaseTool

from focus.config.extensions_config import ExtensionsConfig, get_extensions_config
from focus.mcp.client import build_servers_config
from focus.mcp.diagnostics import McpFailure, connection_reason, server_failure
from focus.security.trust import parse_trust_override, trust_tools

logger = logging.getLogger(__name__)

# client/session 保活注册表：持有引用，防止 GC 回收导致 stdio 子进程终止。
_MCP_CLIENTS: list[object] = []
_MCP_SESSIONS: list["_PersistentMcpSession"] = []
_MCP_CONNECTIONS: dict[tuple, "_PersistentMcpSession"] = {}


class McpTools(list[BaseTool]):
    """保留部分成功工具，同时让缓存识别失败或已断开的连接。"""

    def __init__(self):
        super().__init__()
        self.failed_servers: list[str] = []
        self.failures: list[McpFailure] = []
        self.sessions: list[_PersistentMcpSession] = []

    def record_failure(self, failure: McpFailure) -> None:
        self.failed_servers.append(failure.server_name)
        self.failures.append(failure)

    @property
    def healthy(self) -> bool:
        return not self.failed_servers and all(owner.is_alive for owner in self.sessions)


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

    @property
    def is_alive(self) -> bool:
        return not self._task.done()

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
    _MCP_CONNECTIONS.clear()
    try:
        if sessions:
            await asyncio.gather(*(session.close() for session in sessions))
    finally:
        from focus.mcp.cache import clear_mcp_tools_cache

        clear_mcp_tools_cache()


async def load_mcp_tools(
    servers_config: dict[str, dict],
    overrides: dict[str, dict] | None = None,
    *, sources: dict[str, dict[str, str]] | None = None,
) -> McpTools:
    """连接一组 MCP server；单个 server 失败时跳过，其余 server 继续加载。

    这是外部工具的唯一摄取边界：每个 server 的工具在此按三层信任签发效果契约，
    因此随版本契约覆盖本系统自接的 server，用户覆盖只作用于其显式声明的 server。
    """
    tools = McpTools()
    sources = sources or {}
    if not servers_config:
        return tools
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_mcp_adapters.tools import load_mcp_tools as load_tools_from_session
    except ImportError:
        logger.warning("langchain-mcp-adapters 未安装，MCP 工具不可用")
        for name in servers_config:
            tools.record_failure(server_failure(
                name, sources, stage="connection", reason="langchain-mcp-adapters 未安装，MCP 工具不可用",
                action="安装 Focus 的 MCP 依赖（langchain-mcp-adapters），重启 Focus 后重试。",
            ))
        return tools

    trusted_overrides = overrides or {}
    for server_name, params in servers_config.items():
        try:
            signature = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
            key = (asyncio.get_running_loop(), server_name, signature)
            owner = _MCP_CONNECTIONS.get(key)
            if owner is None or not owner.is_alive:
                client = MultiServerMCPClient({server_name: params})
                owner = _PersistentMcpSession(client, server_name, load_tools_from_session)
                _MCP_CLIENTS.append(client)
                _MCP_SESSIONS.append(owner)
                _MCP_CONNECTIONS[key] = owner
            try:
                server_tools = await owner.start()
            except BaseException:
                await owner.close()
                if _MCP_CONNECTIONS.get(key) is owner:
                    del _MCP_CONNECTIONS[key]
                if owner in _MCP_SESSIONS:
                    index = _MCP_SESSIONS.index(owner)
                    _MCP_SESSIONS.pop(index)
                    _MCP_CLIENTS.pop(index)
                raise
            tools.sessions.append(owner)
            scoped_tools = [item.model_copy(update={"metadata": dict(item.metadata or {})}) for item in server_tools]
            tools.extend(trust_tools(server_name, scoped_tools, trusted_overrides.get(server_name)))
            logger.info("MCP Server '%s' 连接成功，获取 %d 个工具", server_name, len(server_tools))
        except Exception as exc:
            action = (
                "检查 command 程序是否已安装、可执行及 args 是否正确，查看服务日志后重试 MCP。"
                if params.get("transport") == "stdio" else
                "检查 MCP 服务地址、网络连接和认证配置，确认服务可用后重试 MCP。"
            )
            tools.record_failure(server_failure(
                server_name, sources, stage="connection", reason=connection_reason(exc, params), action=action,
            ))
            logger.warning("MCP Server '%s' 连接失败，已跳过", server_name, exc_info=True)
    return tools


async def get_mcp_tools(
    workspace: str | Path | None = None, *, config: ExtensionsConfig | None = None,
) -> McpTools:
    extensions_config = config if config is not None else (
        get_extensions_config("extensions_config.json", workspace=workspace)
        if workspace is not None else get_extensions_config("extensions_config.json")
    )
    servers_config = build_servers_config(extensions_config)

    overrides = {
        name: parse_trust_override(name, server.trust)
        for name, server in extensions_config.mcp_servers.items()
        if server.trust
    }
    tools = await load_mcp_tools(servers_config, overrides, sources=extensions_config.mcp_server_sources)
    for failure in getattr(servers_config, "failures", ()):
        tools.record_failure(failure)
    return tools
