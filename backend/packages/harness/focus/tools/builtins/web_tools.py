"""
本文件对外提供 web_search 与 web_fetch 两个免 API key 的联网内置工具，供 main /
teammate / worker 角色装配（patrol 与承诺层内部 Worker/Evaluator 不装配）。

输入:
    web_search(query, max_results=5) — 关键词与结果数上限；默认实现为 ddgs
        （DuckDuckGo Search 库），经 asyncio.to_thread 异步化
    web_fetch(url) — 目标 http(s) URL；默认实现为 Jina Reader（r.jina.ai），
        返回可读性提取后的 Markdown 文本

输出:
    web_search → 至多 max_results 条结果的文本，每条含序号、标题、完整 URL 与摘要
    web_fetch → 截断至 30000 字符的可读 Markdown；非 http(s) 协议抛 ValueError

具体工作流:
    (1) web_search 校验 max_results（1..10）后在线程池中调用 DDGS().text()
    (2) 搜索成功 → 逐条组装 [N] title / URL / body 文本；无结果或引擎异常 → ToolException
    (3) web_fetch 校验协议白名单后经 httpx 请求 https://r.jina.ai/{url}
    (4) 抓取成功 → 截断返回；网络错误/非 2xx → ToolException
    (5) 两工具的可恢复错误统一转 ToolException，由既有工具错误 middleware
        闭合为匹配原调用 ID 的 ToolMessage，不中断 Agent 主流程

示例:
    text = await web_search.ainvoke({"query": "python asyncio", "max_results": 5})
    md = await web_fetch.ainvoke({"url": "https://docs.python.org/3/"})
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from langchain_core.tools import ToolException, tool

_WEB_FETCH_TIMEOUT_SECONDS = 30
_WEB_FETCH_MAX_CHARS = 30000
_WEB_SEARCH_MAX_RESULTS = 10
_JINA_BASE_URL = "https://r.jina.ai/"


def _truncate(text: str, limit: int = _WEB_FETCH_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n…（内容过长，已截断至 {limit} 字符）"


def _ddgs_search(query: str, max_results: int) -> list[dict[str, Any]]:
    """在线程池中执行 ddgs 同步搜索；引擎异常在此层统一转换为 ToolException。"""
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException, TimeoutException

    try:
        with DDGS(timeout=20) as ddgs:
            results = ddgs.text(
                query,
                region="wt-wt",
                safesearch="moderate",
                max_results=max_results,
            )
    except (DDGSException, TimeoutException) as exc:
        raise ToolException(f"搜索失败：{exc}") from exc
    return results or []


@tool
async def web_search(query: str, max_results: int = 5) -> str:
    """搜索网页并返回标题、URL 与摘要；结果可直接引用。使用默认实现的 DuckDuckGo 搜索，无需 API key。"""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("查询关键词不能为空")
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        raise ValueError("max_results 必须是整数")
    if not 1 <= max_results <= _WEB_SEARCH_MAX_RESULTS:
        raise ValueError(f"max_results 必须在 1..{_WEB_SEARCH_MAX_RESULTS} 之间")
    results = await asyncio.to_thread(_ddgs_search, query.strip(), max_results)
    if not results:
        raise ToolException("搜索无结果，请更换关键词重试")
    lines: list[str] = []
    for index, item in enumerate(results, start=1):
        title = str(item.get("title", "")).strip()
        href = str(item.get("href", "")).strip()
        body = str(item.get("body", "")).strip()
        lines.append(f"[{index}] {title}\nURL: {href}\n{body}")
    return "\n\n".join(lines)


@tool
async def web_fetch(url: str) -> str:
    """抓取单个网页，返回可读性提取后的 Markdown 文本；适用于文档、文章与页面内容。无需 API key。"""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL 不能为空")
    if not url.startswith(("http://", "https://")):
        raise ValueError("只支持 http:// 或 https:// 协议的 URL")
    import httpx

    headers = {"User-Agent": "Mozilla/5.0"}
    api_key = os.environ.get("JINA_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(
            timeout=_WEB_FETCH_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            response = await client.get(f"{_JINA_BASE_URL}{url.strip()}", headers=headers)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ToolException(f"抓取失败：目标返回 HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise ToolException(f"抓取失败：{exc}") from exc
    return _truncate(response.text)
