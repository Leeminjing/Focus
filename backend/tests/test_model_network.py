import asyncio
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openai import DefaultHttpxClient

from focus.config.app_config import AppConfig
from focus.models.factory import create_chat_model
from focus.models.http_clients import (
    ProxyFirstAsyncHttpClient,
    ProxyFirstHttpClient,
)


# OpenAI 2.x 基于 httpx，3.x 基于 httpx2。测试必须使用 SDK 实际的
# HTTP 栈，否则会在开发环境给出假阳性。
_SDK_HTTPX = importlib.import_module(
    DefaultHttpxClient.__mro__[1].__module__.partition(".")[0]
)


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _config() -> AppConfig:
    return AppConfig.model_validate({
        "models": [{
            "name": "deepseek-test",
            "display_name": "DeepSeek test",
            "use": "focus.models.deepseek:DeepSeekChatOpenAI",
            "model": "deepseek-test",
            "api_key": "test-key",
            "base_url": "https://api.deepseek.com",
            "default": True,
        }],
    })


def test_proxy_first_client_falls_back_to_direct_on_connection_failure():
    calls = []

    def proxy(_request):
        calls.append("proxy")
        raise _SDK_HTTPX.ConnectError("proxy unavailable")

    def direct(request):
        calls.append("direct")
        return _SDK_HTTPX.Response(200, json={"route": "direct"}, request=request)

    with ProxyFirstHttpClient(
        proxy_transport=_SDK_HTTPX.MockTransport(proxy),
        direct_transport=_SDK_HTTPX.MockTransport(direct),
    ) as client:
        response = client.get("https://api.deepseek.com/models")

    assert response.json() == {"route": "direct"}
    assert calls == ["proxy", "direct"]


def test_proxy_first_client_keeps_successful_proxy_response():
    calls = []

    def proxy(request):
        calls.append("proxy")
        return _SDK_HTTPX.Response(200, json={"route": "proxy"}, request=request)

    def direct(request):
        calls.append("direct")
        return _SDK_HTTPX.Response(200, json={"route": "direct"}, request=request)

    with ProxyFirstHttpClient(
        proxy_transport=_SDK_HTTPX.MockTransport(proxy),
        direct_transport=_SDK_HTTPX.MockTransport(direct),
    ) as client:
        response = client.get("https://api.deepseek.com/models")

    assert response.json() == {"route": "proxy"}
    assert calls == ["proxy"]


def test_proxy_first_client_does_not_hide_http_errors():
    calls = []

    def proxy(request):
        calls.append("proxy")
        return _SDK_HTTPX.Response(503, request=request)

    def direct(request):
        calls.append("direct")
        return _SDK_HTTPX.Response(200, request=request)

    with ProxyFirstHttpClient(
        proxy_transport=_SDK_HTTPX.MockTransport(proxy),
        direct_transport=_SDK_HTTPX.MockTransport(direct),
    ) as client:
        response = client.get("https://api.deepseek.com/models")

    assert response.status_code == 503
    assert calls == ["proxy"]


def test_proxy_first_async_client_falls_back_to_direct():
    calls = []

    async def proxy(_request):
        calls.append("proxy")
        raise _SDK_HTTPX.ConnectError("proxy unavailable")

    async def direct(request):
        calls.append("direct")
        return _SDK_HTTPX.Response(200, json={"route": "direct"}, request=request)

    async def exercise():
        async with ProxyFirstAsyncHttpClient(
            proxy_transport=_SDK_HTTPX.MockTransport(proxy),
            direct_transport=_SDK_HTTPX.MockTransport(direct),
        ) as client:
            return await client.get("https://api.deepseek.com/models")

    response = asyncio.run(exercise())

    assert response.json() == {"route": "direct"}
    assert calls == ["proxy", "direct"]


def test_factory_installs_proxy_fallback_clients_when_proxy_is_configured(monkeypatch):
    for key in _PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")

    model = create_chat_model(app_config=_config())

    assert isinstance(model.http_client, ProxyFirstHttpClient)
    assert isinstance(model.http_async_client, ProxyFirstAsyncHttpClient)


def test_factory_leaves_sdk_direct_client_unchanged_without_proxy(monkeypatch):
    for key in _PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    model = create_chat_model(app_config=_config())

    assert model.http_client is None
    assert model.http_async_client is None


def test_chat_model_survives_unavailable_proxy_by_calling_provider_directly(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", "0"))
            request_payload = json.loads(self.rfile.read(content_length))
            if request_payload.get("stream"):
                chunks = [
                    {
                        "id": "chatcmpl-focus-test",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "deepseek-test",
                        "choices": [{
                            "index": 0,
                            "delta": {"role": "assistant", "content": "direct fallback worked"},
                            "finish_reason": None,
                        }],
                    },
                    {
                        "id": "chatcmpl-focus-test",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "deepseek-test",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                ]
                body = b"".join(
                    f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks
                ) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            payload = {
                "id": "chatcmpl-focus-test",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-test",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "direct fallback worked"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            pass

    for key in (*_PROXY_ENV_KEYS, "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = _config()
        config.models[0].base_url = f"http://127.0.0.1:{server.server_port}/v1"
        model = create_chat_model(app_config=config, max_retries=0)
        response = model.invoke("ping")

        async def stream_content():
            return "".join([chunk.content async for chunk in model.astream("ping")])

        streamed_content = asyncio.run(stream_content())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.content == "direct fallback worked"
    assert streamed_content == "direct fallback worked"
