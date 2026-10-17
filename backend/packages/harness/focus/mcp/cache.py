"""按有效 MCP 配置缓存工具；内容增改删、环境变量与连接失败均可刷新。

force_refresh 提供显式重试。部分成功结果保留可用工具，但不会阻止下次重试。
同配置的并发请求共享一次有界初始化；取消等待者不会关闭共享连接。
工具快照和已无 Run 引用的连接按空闲期回收。
"""

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from langchain_core.tools import BaseTool

from focus.config.extensions_config import get_extensions_config
from focus.mcp.tools import MCP_SESSION_IDLE_TIMEOUT, get_mcp_tools, retain_mcp_tools

logger = logging.getLogger(__name__)

_cache: dict[tuple, list[BaseTool]] = {}
_loads: dict[tuple, asyncio.Task] = {}
_expiry: dict[tuple, asyncio.TimerHandle] = {}


def _discard(key) -> None:
    _cache.pop(key, None)
    timer = _expiry.pop(key, None)
    if timer is not None:
        timer.cancel()


def discard_mcp_session(owner) -> None:
    """关闭连接时也移除引用它的工具快照，避免配置变更累积闭包。"""
    for key, tools in list(_cache.items()):
        if owner in getattr(tools, "sessions", ()):
            _discard(key)


def _touch(key) -> None:
    timer = _expiry.pop(key, None)
    if timer is not None:
        timer.cancel()
    _expiry[key] = asyncio.get_running_loop().call_later(MCP_SESSION_IDLE_TIMEOUT, _discard, key)


def clear_mcp_tools_cache() -> None:
    """随应用关闭释放工具快照与事件循环引用。"""
    _cache.clear()
    for task in _loads.values():
        task.cancel()
    _loads.clear()
    for timer in _expiry.values():
        timer.cancel()
    _expiry.clear()


async def get_mcp_tools_cached(
    workspace: str | Path | None = None, *, force_refresh: bool = False,
) -> list[BaseTool]:
    # 配置先解析为实际值再取摘要，不把环境变量引用或秘密值存入缓存键。
    config = get_extensions_config("extensions_config.json", workspace=workspace)
    snapshot = {"config": config.model_dump(), "sources": config.mcp_server_sources, "errors": config.mcp_server_errors}
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    key = (asyncio.get_running_loop(), digest)
    cached = _cache.get(key)
    if not force_refresh and cached is not None and getattr(cached, "healthy", True):
        _touch(key)
        retain_mcp_tools(cached)
        return cached

    async def load():
        tools = await get_mcp_tools(config=config)
        _cache[key] = tools
        _touch(key)
        if not getattr(tools, "healthy", True):
            logger.warning("MCP 配置或连接失败，下次装配将自动重试")
        return tools

    pending = _loads.get(key)
    if pending is None:
        pending = asyncio.create_task(load(), name="focus-mcp-assembly")
        _loads[key] = pending

        def finished(task):
            if _loads.get(key) is task:
                del _loads[key]
            if not task.cancelled():
                task.exception()

        pending.add_done_callback(finished)
    tools = await asyncio.shield(pending)
    retain_mcp_tools(tools)
    return tools
