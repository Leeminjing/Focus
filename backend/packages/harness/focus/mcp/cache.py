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
from focus.mcp.tools import get_mcp_tools

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
    config = get_extensions_config("extensions_config.json", workspace=workspace)
    digest = hashlib.sha256(json.dumps(config.model_dump(), sort_keys=True).encode()).hexdigest()
    key = (asyncio.get_running_loop(), digest)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cache.get(key)
        if not force_refresh and cached is not None and getattr(cached, "healthy", True):
            return cached
        tools = await get_mcp_tools(config=config)
        _cache[key] = tools
        if not getattr(tools, "healthy", True):
            logger.warning("MCP 部分连接失败，下次装配将自动重试")
        return tools
