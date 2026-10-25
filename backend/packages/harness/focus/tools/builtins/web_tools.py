"""通过 DeepSeek Search / Fetch 提供联网工具，保持 Agent 的工具与效果契约。

输入为 web_search(query, max_results=5) 或 web_fetch(url)，使用独立的
DEEPSEEK_API_KEY，与 Run 选择的模型无关。搜索返回编号、标题、完整 URL 与摘要；
抓取返回至多 30000 字符的 Markdown 正文及必要的截断提示。

工作流：校验参数 → 异步 POST DeepSeek JSON API → 转换结果。缺少凭据、HTTP / 网络
错误及无效响应统一抛 ToolException，由现有工具错误处理闭合调用，不中断 Agent。
两个工具仅访问网络并返回内存文本，无受治理的本地副作用；main / teammate / worker
继续装配，patrol 与承诺层内部 Worker/Evaluator 不直接装配。

示例：await web_search.ainvoke({"query": "python asyncio", "max_results": 5})。
"""

from __future__ import annotations

from typing import Any

import httpx
from focus.config.web_config import get_deepseek_api_key
from focus.security.effects import NO_LOCAL_EFFECT, declare_effect
from httpx import AsyncClient
from langchain_core.tools import ToolException, tool

_WEB_TIMEOUT_SECONDS = 30
_WEB_FETCH_MAX_CHARS = 30000
_WEB_SEARCH_MAX_RESULTS = 10
_WEB_SEARCH_MAX_QUERY_CHARS = 128
_DEEPSEEK_TOOLS_BASE_URL = "https://api.deepseek.com/v1/tools"
_HTTP_ERROR_HINTS = {
    400: "请求参数无效，请调整查询或 URL",
    401: "鉴权失败，请检查 DEEPSEEK_API_KEY",
    402: "DeepSeek 账户余额不足，请充值后重试",
    403: "请求被 DeepSeek 安全策略拒绝，请尝试其他 URL",
    404: "未能提取可用网页正文，请尝试其他 URL",
    408: "请求超时，请稍后重试",
    413: "请求内容过大，请缩短查询或 URL",
    429: "DeepSeek 请求限流，请稍后重试",
    504: "DeepSeek 上游请求超时，请稍后重试",
}


def _truncate(text: str, limit: int = _WEB_FETCH_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n…（内容过长，已截断至 {limit} 字符）"


async def _deepseek_request(endpoint: str, payload: dict[str, Any], *, action: str) -> dict[str, Any]:
    """共用鉴权与错误边界；诊断不包含凭据、响应正文或底层异常文本。"""
    try:
        api_key = get_deepseek_api_key()
    except ValueError as exc:
        raise ToolException(f"{action}失败：{exc}") from exc
    try:
        async with AsyncClient(timeout=_WEB_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{_DEEPSEEK_TOOLS_BASE_URL}/{endpoint}",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        hint = _HTTP_ERROR_HINTS.get(status, "DeepSeek 服务暂不可用，请稍后重试")
        raise ToolException(f"{action}失败：HTTP {status}，{hint}") from exc
    except httpx.TimeoutException as exc:
        raise ToolException(f"{action}失败：DeepSeek 请求超时，请稍后重试") from exc
    except httpx.HTTPError as exc:
        raise ToolException(f"{action}失败：无法连接 DeepSeek，请检查网络后重试") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise ToolException(f"{action}失败：DeepSeek 返回的数据格式无效") from exc
    if not isinstance(data, dict):
        raise ToolException(f"{action}失败：DeepSeek 返回的数据格式无效")
    return data


@tool
async def web_search(query: str, max_results: int = 5) -> str:
    """搜索网页并返回标题、URL 与摘要；结果可直接引用。查询限 128 字符，使用 DeepSeek 搜索。"""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("查询关键词不能为空")
    query = query.strip()
    if len(query) > _WEB_SEARCH_MAX_QUERY_CHARS:
        raise ValueError(f"查询关键词不能超过 {_WEB_SEARCH_MAX_QUERY_CHARS} 个 Unicode 字符，请缩短查询")
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        raise ValueError("max_results 必须是整数")  # noqa: TRY004 — 保持现有工具参数错误契约
    if not 1 <= max_results <= _WEB_SEARCH_MAX_RESULTS:
        raise ValueError(f"max_results 必须在 1..{_WEB_SEARCH_MAX_RESULTS} 之间")
    data = await _deepseek_request(
        "search", {"query": query, "count": max_results, "need_content": False}, action="搜索"
    )
    results = data.get("results")
    if not isinstance(results, list):
        raise ToolException("搜索失败：DeepSeek 返回的数据格式无效")
    if not results:
        raise ToolException("搜索无结果，请更换关键词重试")
    lines: list[str] = []
    for index, item in enumerate(results[:max_results], start=1):
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("title", "url")):
            raise ToolException("搜索失败：DeepSeek 返回的数据格式无效")
        body = item.get("summary") or item.get("snippet", "")
        if not isinstance(body, str):
            raise ToolException("搜索失败：DeepSeek 返回的摘要格式无效")
        lines.append(f"[{index}] {item['title'].strip()}\nURL: {item['url'].strip()}\n{body.strip()}")
    return "\n\n".join(lines)


@tool
async def web_fetch(url: str) -> str:
    """抓取单个网页，返回可读性提取后的 Markdown 文本；适用于文档、文章与页面内容。使用 DeepSeek 抓取。"""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL 不能为空")
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("只支持 http:// 或 https:// 协议的 URL")
    data = await _deepseek_request("fetch", {"url": url}, action="抓取")
    content = data.get("content")
    if not isinstance(content, str):
        raise ToolException("抓取失败：DeepSeek 返回的正文格式无效")
    if not content.strip():
        raise ToolException("抓取失败：DeepSeek 未返回可用的网页正文，请尝试其他 URL")
    return _truncate(content)


declare_effect(web_search, NO_LOCAL_EFFECT)
declare_effect(web_fetch, NO_LOCAL_EFFECT)

WEB_TOOLS: list[Any] = [web_search, web_fetch]
