"""
本文件对外提供阶段四使用的 find_reference_urls 工具和搜索结果解析器。

输入:
    query — 用户提到但未提供完整 URL 的网站、文档或项目名称。

输出:
    list[dict[str, str]] — 包含候选 title 和 url 的搜索结果列表。

具体工作流:
    (1) 对查询文本执行 URL 编码并请求搜索结果页面。
    (2) _SearchResultParser 提取结果链接和标题。
    (3) 过滤无效地址并返回有限数量的候选项。
    (4) 网络或解析失败时返回空列表，由阶段四进入人工确认。

示例:
    candidates = await find_reference_urls.ainvoke({"query": "React docs"})
"""

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import httpx
from langchain.tools import tool


class _SearchResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and "result__a" in values.get("class", ""):
            self._href = values.get("href", "") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._href:
            return
        query = parse_qs(urlparse(self._href).query)
        url = query.get("uddg", [self._href])[0]
        title = " ".join(self._text).strip()
        if title and url.startswith(("http://", "https://")):
            self.results.append({"title": title, "url": url})
        self._href = ""
        self._text = []


@tool
async def find_reference_urls(query: str) -> list[dict[str, str]]:
    """Search the web for candidate URLs that the user mentioned without a URL."""
    if not query.strip():
        return []
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            response = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return [{"error": f"搜索失败: {exc}"}]
    parser = _SearchResultParser()
    parser.feed(response.text)
    return parser.results[:5]
