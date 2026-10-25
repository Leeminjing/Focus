"""DeepSeek 联网工具的离线请求、结果、失败与实际 Agent 工具交换回归。

通过 MockTransport 验证公开工具入口与真实执行图，不访问外部 API 或用户数据库。
"""

import asyncio
import json

import httpx
import pytest
from focus.config.layered import load_global_dotenv
from focus.config.web_config import get_deepseek_api_key
from focus.tools.builtins import web_tools
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import ToolException
from langgraph.checkpoint.memory import InMemorySaver


@pytest.fixture
def deepseek_http(monkeypatch):
    """只替换 HTTP 传输，保留真实 httpx JSON 编解码与状态处理。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-search-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-model-test")
    real_client = httpx.AsyncClient
    requests = []

    def install(handler):
        def capture(request):
            requests.append(request)
            assert request.method == "POST"
            assert request.url.host == "api.deepseek.com"
            assert request.headers["Authorization"] == "Bearer sk-search-test"
            assert request.headers["Content-Type"] == "application/json"
            assert request.extensions["timeout"]["read"] == 30
            return handler(request)

        monkeypatch.setattr(web_tools, "AsyncClient", lambda **kwargs: real_client(
            **kwargs, transport=httpx.MockTransport(capture), trust_env=False,
        ))
        return requests

    return install


def _result(**overrides):
    return {"title": " Python asyncio ", "url": " https://docs.python.org/3/library/asyncio.html ",
            "summary": " Long summary ", "snippet": "Short snippet", "content": "", **overrides}


def test_public_tool_contract():
    assert web_tools.WEB_TOOLS == [web_tools.web_search, web_tools.web_fetch]
    assert web_tools.web_search.name == "web_search" and web_tools.web_fetch.name == "web_fetch"
    assert set(web_tools.web_search.args) == {"query", "max_results"}
    assert web_tools.web_search.args["max_results"]["default"] == 5
    assert set(web_tools.web_fetch.args) == {"url"}


def test_search_request_format_defaults_and_result_cap(deepseek_http):
    requests = deepseek_http(lambda request: httpx.Response(200, json={"results": [_result()] * 8}))
    output = asyncio.run(web_tools.web_search.ainvoke({"query": "  Python asyncio  "}))
    assert str(requests[0].url) == "https://api.deepseek.com/v1/tools/search"
    assert json.loads(requests[0].content) == {"query": "Python asyncio", "count": 5, "need_content": False}
    assert output.startswith("[1] Python asyncio\nURL: https://docs.python.org/3/library/asyncio.html\nLong summary")
    assert "[5]" in output and "[6]" not in output and "Short snippet" not in output


def test_search_uses_snippet_when_summary_is_empty(deepseek_http):
    deepseek_http(lambda request: httpx.Response(200, json={"results": [_result(summary="")]}))
    assert asyncio.run(web_tools.web_search.ainvoke({"query": "python", "max_results": 1})).endswith("Short snippet")


def test_search_accepts_unicode_query_and_maximum_count(deepseek_http):
    requests = deepseek_http(lambda request: httpx.Response(200, json={"results": [_result()]}))
    asyncio.run(web_tools.web_search.ainvoke({"query": "搜🔍" * 64, "max_results": 10}))
    assert json.loads(requests[0].content)["query"] == "搜🔍" * 64
    assert json.loads(requests[0].content)["count"] == 10


@pytest.mark.parametrize("args, message", [
    ({"query": "  "}, "不能为空"),
    ({"query": "搜" * 129}, "128"),
    ({"query": "python", "max_results": 0}, "1..10"),
    ({"query": "python", "max_results": 11}, "1..10"),
])
def test_search_validates_before_network(deepseek_http, args, message):
    requests = deepseek_http(lambda request: pytest.fail("invalid input must not send a request"))
    with pytest.raises(ValueError, match=message):
        asyncio.run(web_tools.web_search.ainvoke(args))
    assert not requests


def test_search_empty_results(deepseek_http):
    deepseek_http(lambda request: httpx.Response(200, json={"results": []}))
    with pytest.raises(ToolException, match="搜索无结果"):
        asyncio.run(web_tools.web_search.ainvoke({"query": "python"}))


@pytest.mark.parametrize("size", [20, 30000, 30001])
def test_fetch_request_markdown_and_truncation(deepseek_http, size):
    content = "文" * size
    requests = deepseek_http(lambda request: httpx.Response(200, json={"title": "Page", "content": content}))
    output = asyncio.run(web_tools.web_fetch.ainvoke({"url": "  https://example.com/article  "}))
    assert str(requests[0].url) == "https://api.deepseek.com/v1/tools/fetch"
    assert json.loads(requests[0].content) == {"url": "https://example.com/article"}
    if size <= 30000:
        assert output == content
    else:
        assert output == content[:30000] + "\n\n…（内容过长，已截断至 30000 字符）"


@pytest.mark.parametrize("url", ["", "  ", "file:///etc/passwd", "ftp://example.com"])
def test_fetch_validates_before_network(deepseek_http, url):
    requests = deepseek_http(lambda request: pytest.fail("invalid input must not send a request"))
    with pytest.raises(ValueError):
        asyncio.run(web_tools.web_fetch.ainvoke({"url": url}))
    assert not requests


_TOOLS = [(web_tools.web_search, {"query": "python"}),
          (web_tools.web_fetch, {"url": "https://example.com"})]


@pytest.mark.parametrize("tool, args", _TOOLS)
@pytest.mark.parametrize("key", [None, "", "   "])
def test_missing_key_is_recoverable_and_does_not_use_model_key(monkeypatch, deepseek_http, tool, args, key):
    requests = deepseek_http(lambda request: pytest.fail("missing key must not send a request"))
    if key is None:
        monkeypatch.delenv("DEEPSEEK_API_KEY")
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    with pytest.raises(ToolException, match="DEEPSEEK_API_KEY"):
        asyncio.run(tool.ainvoke(args))
    assert not requests


@pytest.mark.parametrize("tool, args", _TOOLS)
@pytest.mark.parametrize("status, hint", [(400, "参数"), (401, "鉴权"), (402, "余额"),
    (403, "安全策略"), (404, "正文"), (408, "超时"), (413, "过大"), (429, "限流"),
    (500, "稍后重试"), (503, "稍后重试"), (504, "超时")])
def test_http_errors_are_actionable_without_response_body(deepseek_http, tool, args, status, hint):
    requests = deepseek_http(lambda request: httpx.Response(status, text="sensitive upstream message sk-search-test"))
    with pytest.raises(ToolException) as caught:
        asyncio.run(tool.ainvoke(args))
    assert str(status) in str(caught.value) and hint in str(caught.value)
    assert "sensitive" not in str(caught.value) and "sk-search-test" not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.parametrize("tool, args", _TOOLS)
@pytest.mark.parametrize("exception, hint", [(httpx.ReadTimeout, "超时"), (httpx.ConnectTimeout, "超时"),
    (httpx.ConnectError, "网络"), (httpx.RemoteProtocolError, "网络")])
def test_network_errors_are_recoverable(deepseek_http, tool, args, exception, hint):
    def handle(request):
        raise exception("sensitive exception sk-search-test", request=request)
    deepseek_http(handle)
    with pytest.raises(ToolException, match=hint) as caught:
        asyncio.run(tool.ainvoke(args))
    assert "sensitive" not in str(caught.value) and "sk-search-test" not in str(caught.value)


@pytest.mark.parametrize("tool, args", _TOOLS)
@pytest.mark.parametrize("body", ["not JSON", "null", "[]", "{}"])
def test_invalid_response_is_recoverable(deepseek_http, tool, args, body):
    deepseek_http(lambda request: httpx.Response(200, text=body))
    with pytest.raises(ToolException, match="格式无效"):
        asyncio.run(tool.ainvoke(args))


@pytest.mark.parametrize("result", [None, "bad", {}, _result(title=None), _result(url=[]), _result(summary={"bad": True})])
def test_malformed_search_result_is_recoverable(deepseek_http, result):
    deepseek_http(lambda request: httpx.Response(200, json={"results": [result]}))
    with pytest.raises(ToolException, match="格式无效"):
        asyncio.run(web_tools.web_search.ainvoke({"query": "python"}))


@pytest.mark.parametrize("content", [None, 1, [], "", "  "])
def test_unusable_fetch_content_is_recoverable(deepseek_http, content):
    deepseek_http(lambda request: httpx.Response(200, json={"content": content}))
    with pytest.raises(ToolException):
        asyncio.run(web_tools.web_fetch.ainvoke({"url": "https://example.com"}))


def test_global_dotenv_supplies_independent_key_with_environment_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-file\n", encoding="utf-8")
    load_global_dotenv()
    assert get_deepseek_api_key() == "sk-file"
    monkeypatch.setenv("DEEPSEEK_API_KEY", " sk-env ")
    load_global_dotenv()
    assert get_deepseek_api_key() == "sk-env"


@pytest.mark.parametrize("search_status", [200, 401, 402, 429, "empty", "timeout", "missing"])
def test_real_agent_search_fetch_citation_and_error_recovery(monkeypatch, tmp_path, deepseek_http, search_status):
    """OpenAI 模型的真实工厂调用 DeepSeek 工具，错误闭合后仍可抓取并回答。"""
    from focus.models.provider_contract import ProviderContract
    from focus.models.responses import FocusResponsesChatModel

    from backend.tests.runtime_context_support import runtime_context
    from backend.tests.tool_catalog_support import (
        app_config,
        desktop_service,
        install_fixture,
        registry_for,
    )

    async def run():
        captured = []
        url = "https://docs.python.org/3/library/asyncio.html"
        answer = ("Please configure the web tool key." if search_status == "missing"
                  else f"asyncio supports asynchronous I/O. [Python asyncio]({url})")

        def model_response(request):
            captured.append(json.loads(request.content))
            step = len(captured)
            if step > 1:
                tool_outputs = [item for item in captured[-1]["input"] if item.get("type") == "function_call_output"]
                assert tool_outputs and tool_outputs[-1]["call_id"] == f"call-{step - 1}"
                if step == 2 and search_status == 200:
                    assert url in tool_outputs[-1]["output"]
                if step == 3 and search_status != "missing":
                    assert "Asynchronous I/O." in tool_outputs[-1]["output"]
            if step <= 2:
                name, args = (("web_search", {"query": "Python asyncio"}) if step == 1
                              else ("web_fetch", {"url": url}))
                output = [{"type": "function_call", "id": f"invoke-{step}", "call_id": f"call-{step}",
                           "status": "completed", "name": name, "arguments": json.dumps(args)}]
            else:
                output = [{"type": "message", "id": "answer", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": answer, "annotations": []}]}]
            return httpx.Response(200, json={"object": "response", "id": f"response-{step}", "created_at": 0,
                "model": "fixture", "status": "completed", "output": output})

        # 先构造模型专用传输，联网工具独立使用另一传输。
        client = httpx.AsyncClient(transport=httpx.MockTransport(model_response))
        model = FocusResponsesChatModel(model="fixture", api_key="sk-model-test", base_url="https://fixture.test/v1",
            provider_contract=ProviderContract("openai", "responses", context_window=1000000), http_async_client=client)

        def web_response(request):
            if request.url.path.endswith("/search"):
                if search_status == "timeout":
                    raise httpx.ReadTimeout("upstream timed out", request=request)
                status = search_status if isinstance(search_status, int) else 200
                return httpx.Response(status, json={"results": [] if search_status == "empty" else [_result()]})
            assert json.loads(request.content) == {"url": url}
            return httpx.Response(200, json={"title": "asyncio", "content": "# asyncio\nAsynchronous I/O."})

        requests = deepseek_http(web_response)
        if search_status == "missing":
            monkeypatch.delenv("DEEPSEEK_API_KEY")
        install_fixture(monkeypatch, registry_for([]), model)
        try:
            service = desktop_service(app_config("openai", False))
            graph = await service._build_agent_factory("task", "main", str(tmp_path),
                {"permissions": ["read"], "model_name": "fixture"}, "base", "", "main")()
            graph.checkpointer = InMemorySaver()
            result = await graph.ainvoke({"messages": [HumanMessage(content="Search, fetch, and cite Python asyncio.")]},
                {"configurable": {"thread_id": "web-tools"}},
                context=runtime_context(agent_id="main", task_id="task", workspace=str(tmp_path)))
            messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
            assert [message.tool_call_id for message in messages] == ["call-1", "call-2"]
            expected = {200: url, "empty": "搜索无结果", "timeout": "超时", "missing": "DEEPSEEK_API_KEY"}
            assert expected.get(search_status, str(search_status)) in messages[0].content
            assert messages[0].status == ("success" if search_status == 200 else "error")
            if search_status == "missing":
                assert "DEEPSEEK_API_KEY" in messages[1].content and not requests
            else:
                assert "Asynchronous I/O." in messages[1].content and len(requests) == 2
            assert len(captured) == 3
            assert result["messages"][-1].content == answer
        finally:
            await client.aclose()
            model._client.close()

    asyncio.run(run())
