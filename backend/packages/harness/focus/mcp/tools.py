"""MCP 工具的配置、连接与信任摄取边界。

get_mcp_tools(workspace=..., config=...) 接受工作区或已经冻结解析的配置。
load_mcp_tools 按 server 名与连接参数复用当前事件循环的常驻 session；
每个 session 在独立 owner task 内进入和退出，避免污染请求任务的 AnyIO 栈。
工具副本按当前配置签发信任，防止工作区覆盖修改其他 Run 的工具契约。

McpTools 保留部分成功列表，并通过 failed_servers / healthy 向缓存报告可重试状态。
初始化共用装配截止时间，并限制当前事件循环的连接并发。
成功 session 由 Run 引用保活，最后一个 Run 结束后按空闲期回收。
"""

import asyncio
import hashlib
import json
import logging
from contextvars import ContextVar, Token
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
_MCP_LIMITERS: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}

MCP_INITIALIZATION_TIMEOUT = 10.0
MCP_MAX_CONCURRENT_INITIALIZATIONS = 4
MCP_SESSION_IDLE_TIMEOUT = 300.0
MCP_SESSION_CLOSE_TIMEOUT = 5.0


class McpRunLease:
    """一次 Run 对全部已装配连接的引用；子任务共享此引用集合。"""

    def __init__(self):
        self.sessions: set[_PersistentMcpSession] = set()
        self.closed = False

    def retain(self, sessions) -> None:
        if self.closed:
            return
        for owner in sessions:
            if owner not in self.sessions:
                self.sessions.add(owner)
                owner.acquire()

    def close(self) -> None:
        self.closed = True
        for owner in self.sessions:
            owner.release()
        self.sessions.clear()


_RUN_LEASE: ContextVar[McpRunLease | None] = ContextVar("mcp_run_lease", default=None)


def begin_mcp_run() -> Token:
    return _RUN_LEASE.set(McpRunLease())


def end_mcp_run(token: Token) -> None:
    lease = _RUN_LEASE.get()
    if lease is not None:
        lease.close()
    _RUN_LEASE.reset(token)


def retain_mcp_tools(tools) -> None:
    sessions = getattr(tools, "sessions", ())
    lease = _RUN_LEASE.get()
    if lease is not None:
        lease.retain(sessions)
    for owner in sessions:
        owner.touch()


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

    def __init__(self, client, server_name: str, load_tools_from_session, *, key: tuple, deadline: float):
        loop = asyncio.get_running_loop()
        self._client = client
        self._server_name = server_name
        self._load_tools = load_tools_from_session
        self._key = key
        self._ready = loop.create_future()
        # 即使所有等待者已取消，初始化异常也必须被取走。
        self._ready.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        self._stop = asyncio.Event()
        self._references = 0
        self._idle_timer = None
        self._close_timer = None
        self._deadline_timer = loop.call_at(deadline, self._expire)
        self._task = loop.create_task(
            self._run(), name=f"focus-mcp-session-{server_name}"
        )

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        limiter = _MCP_LIMITERS.setdefault(loop, asyncio.Semaphore(MCP_MAX_CONCURRENT_INITIALIZATIONS))
        acquired = False
        try:
            await limiter.acquire()
            acquired = True
            async with self._client.session(self._server_name) as session:
                tools = await self._load_tools(session)
                if not self._ready.done():
                    self._ready.set_result(tools)
                self._deadline_timer.cancel()
                limiter.release()
                acquired = False
                self.touch()
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
        finally:
            if acquired:
                limiter.release()
            self._deadline_timer.cancel()
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            if self._close_timer is not None:
                self._close_timer.cancel()
            self._forget()

    def _forget(self) -> None:
        if _MCP_CONNECTIONS.get(self._key) is self:
            del _MCP_CONNECTIONS[self._key]
        if self in _MCP_SESSIONS:
            _MCP_SESSIONS.remove(self)
            _MCP_CLIENTS.remove(self._client)
        from focus.mcp.cache import discard_mcp_session

        discard_mcp_session(self)

    def _expire(self) -> None:
        if not self._ready.done():
            self._ready.set_exception(TimeoutError("MCP 初始化超过统一截止时间（包括连接排队、握手和工具发现）"))
            self._task.cancel()
            self._close_timer = asyncio.get_running_loop().call_later(MCP_SESSION_CLOSE_TIMEOUT, self._task.cancel)

    def acquire(self) -> None:
        self._references += 1
        if self._idle_timer is not None:
            self._idle_timer.cancel()

    def release(self) -> None:
        self._references -= 1
        self.touch()

    def touch(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        if not self._references and self.is_alive:
            self._idle_timer = asyncio.get_running_loop().call_later(MCP_SESSION_IDLE_TIMEOUT, self._retire)

    def _retire(self) -> None:
        # 先停止复用，关闭仍在进入 session 的 owner task 中完成。
        self._stop.set()
        self._close_timer = asyncio.get_running_loop().call_later(MCP_SESSION_CLOSE_TIMEOUT, self._task.cancel)

    async def start(self) -> list[BaseTool]:
        """等待 session 初始化；shield 防止请求取消直接取消共享 future。"""
        return await asyncio.shield(self._ready)

    @property
    def is_alive(self) -> bool:
        return not self._task.done() and not self._stop.is_set() and not (
            self._ready.done() and (self._ready.cancelled() or self._ready.exception() is not None)
        )

    async def close(self) -> None:
        """通知 owner task 退出，使 session 在进入它的同一任务内关闭。"""
        if not self._ready.done():
            self._task.cancel()
        else:
            self._stop.set()
        done, _ = await asyncio.wait([self._task], timeout=MCP_SESSION_CLOSE_TIMEOUT)
        if not done:
            self._task.cancel()
            done, _ = await asyncio.wait([self._task], timeout=MCP_SESSION_CLOSE_TIMEOUT)
            if not done:
                logger.warning("MCP Server '%s' session 关闭超时", self._server_name)


async def close_mcp_sessions() -> None:
    """关闭并清空当前进程持有的全部 MCP 常驻 session。"""
    from focus.mcp.cache import clear_mcp_tools_cache

    clear_mcp_tools_cache()
    sessions = list(_MCP_SESSIONS)
    _MCP_SESSIONS.clear()
    _MCP_CLIENTS.clear()
    _MCP_CONNECTIONS.clear()
    _MCP_LIMITERS.clear()
    try:
        if sessions:
            await asyncio.gather(*(session.close() for session in sessions))
    finally:
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
    deadline = asyncio.get_running_loop().time() + MCP_INITIALIZATION_TIMEOUT

    async def load_server(server_name, params):
        result = McpTools()
        try:
            signature = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
            key = (asyncio.get_running_loop(), server_name, signature)
            owner = _MCP_CONNECTIONS.get(key)
            if owner is None or not owner.is_alive:
                client = MultiServerMCPClient({server_name: params})
                owner = _PersistentMcpSession(client, server_name, load_tools_from_session, key=key, deadline=deadline)
                _MCP_CLIENTS.append(client)
                _MCP_SESSIONS.append(owner)
                _MCP_CONNECTIONS[key] = owner
            server_tools = await owner.start()
            result.sessions.append(owner)
            scoped_tools = [item.model_copy(update={"metadata": dict(item.metadata or {})}) for item in server_tools]
            result.extend(trust_tools(server_name, scoped_tools, trusted_overrides.get(server_name)))
            logger.info("MCP Server '%s' 连接成功，获取 %d 个工具", server_name, len(server_tools))
        except Exception as exc:
            action = (
                "检查 command 程序是否已安装、可执行及 args 是否正确，查看服务日志后重试 MCP。"
                if params.get("transport") == "stdio" else
                "检查 MCP 服务地址、网络连接和认证配置，确认服务可用后重试 MCP。"
            )
            result.record_failure(server_failure(
                server_name, sources, stage="connection", reason=connection_reason(exc, params), action=action,
            ))
            logger.warning("MCP Server '%s' 连接失败，已跳过", server_name, exc_info=True)
        return result

    tasks = {name: asyncio.create_task(load_server(name, params)) for name, params in servers_config.items()}
    try:
        # wait 不等待被取消任务清理网络/子进程，装配耗时始终由同一截止时间控制。
        await asyncio.wait(tasks.values(), timeout=max(0, deadline - asyncio.get_running_loop().time()))
        for name, task in tasks.items():
            if task.done():
                result = task.result()
                tools.extend(result)
                tools.sessions.extend(result.sessions)
                for failure in result.failures:
                    tools.record_failure(failure)
            else:
                tools.record_failure(server_failure(
                    name, sources, stage="connection",
                    reason="MCP 初始化超过统一截止时间（包括连接排队、握手和工具发现）",
                    action="检查服务启动、网络和认证配置后单独重试 MCP；已成功的连接会复用。",
                ))
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    retain_mcp_tools(tools)
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
