"""按有效 MCP 配置缓存工具；内容增改删、环境变量与连接失败均可刷新。

force_refresh 提供显式重试。部分成功结果保留可用工具，但不会阻止下次重试。
旧配置的 session 保留给已开始的 Run，在 close_mcp_sessions 时统一关闭。
"""

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from langchain_core.tools import BaseTool

from focus.config.extensions_config import get_extensions_config
from focus.config.layered import LayeredConfigError
from focus.mcp.diagnostics import McpFailure
from focus.mcp.tools import McpTools, get_mcp_tools

logger = logging.getLogger(__name__)

_cache: dict[tuple, list[BaseTool]] = {}
_locks: dict[tuple, asyncio.Lock] = {}


def clear_mcp_tools_cache() -> None:
    """随应用关闭释放工具快照与事件循环引用。"""
    _cache.clear()
    _locks.clear()


async def get_mcp_tools_cached(
    workspace: str | Path | None = None, *, force_refresh: bool = False,
) -> list[BaseTool]:
    # 配置先解析为实际值再取摘要，不把环境变量引用或秘密值存入缓存键。
    try:
        config = get_extensions_config("extensions_config.json", workspace=workspace)
    except LayeredConfigError as exc:
        # 整层损坏时无法知道哪些 server 被覆盖，禁止继续连接低优先级配置。
        # 不缓存错误；文件修复后的下一次装配会重新读取。
        tools = McpTools()
        tools.record_failure(McpFailure(
            server_name=Path(exc.config_path).name, scope=exc.scope, config_path=exc.config_path,
            stage="configuration", reason=exc.reason, action=exc.action,
        ))
        return tools
    snapshot = {"config": config.model_dump(), "sources": config.mcp_server_sources, "errors": config.mcp_server_errors}
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    key = (asyncio.get_running_loop(), digest)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cache.get(key)
        if not force_refresh and cached is not None and getattr(cached, "healthy", True):
            return cached
        tools = await get_mcp_tools(config=config)
        _cache[key] = tools
        if not getattr(tools, "healthy", True):
            logger.warning("MCP 配置或连接失败，下次装配将自动重试")
        return tools
