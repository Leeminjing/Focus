"""OpenAI 兼容模型的代理优先、直连回退 HTTP 客户端。"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Mapping
from typing import Any

from openai import DefaultAsyncHttpxClient, DefaultHttpxClient
from focus.models.telemetry import RequestTiming


logger = logging.getLogger(__name__)

# OpenAI 2.x 的默认客户端继承 httpx.Client，3.x 则继承 httpx2.Client。
# 以 SDK 实际使用的基类解析 HTTP 栈，避免混用两套 Request/URL/异常类型。
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


def environment_proxy_configured(environ: Mapping[str, str] | None = None) -> bool:
    """返回当前进程是否显式配置了 HTTP(S) 代理。"""
    values = os.environ if environ is None else environ
    return any(values.get(key) for key in _PROXY_ENV_KEYS)


def _copy_request(request: Any, content: bytes) -> Any:
    """复制可重放的模型请求，保留 OpenAI 注入的头与超时扩展。"""
    return request.__class__(
        method=request.method,
        url=request.url,
        headers=request.headers,
        content=content,
        extensions=dict(request.extensions),
    )


class _TimedSyncStream(_SDK_HTTPX.SyncByteStream):
    def __init__(self, stream, timing):
        self.stream, self.timing = stream, timing
        self.complete = False

    def __iter__(self):
        try:
            yield from self.stream
            self.complete = True
            self.timing.finish("error" if self.timing.status >= 400 else "success")
        except BaseException as error:
            self.timing.finish("error", error)
            raise

    def close(self):
        try:
            self.stream.close()
        finally:
            self.timing.finish("success" if self.complete else "closed_early")


class _TimedAsyncStream(_SDK_HTTPX.AsyncByteStream):
    def __init__(self, stream, timing):
        self.stream, self.timing = stream, timing
        self.complete = False

    async def __aiter__(self):
        import asyncio
        try:
            async for chunk in self.stream:
                yield chunk
            self.complete = True
            self.timing.finish("error" if self.timing.status >= 400 else "success")
        except BaseException as error:
            self.timing.finish("cancelled" if isinstance(error, asyncio.CancelledError) else "error", error)
            raise

    async def aclose(self):
        try:
            await self.stream.aclose()
        finally:
            self.timing.finish("success" if self.complete else "closed_early")


class ObservedHttpClient(DefaultHttpxClient):
    def __init__(self, *, performance_identity=None, **kwargs):
        self.performance_identity = performance_identity
        hooks = {name: list(values) for name, values in kwargs.pop("event_hooks", {}).items()}
        hooks["response"] = [self._headers, *hooks.get("response", [])]
        kwargs["event_hooks"] = hooks
        super().__init__(**kwargs)

    @staticmethod
    def _headers(response):
        timing = response.request.extensions.get("focus_request_timing")
        if timing is not None:
            timing.headers(response)

    def send(self, request, *args, **kwargs):
        timing = RequestTiming(request, self.performance_identity)
        request.extensions["focus_request_timing"] = timing
        try:
            response = super().send(request, *args, **kwargs)
            if response.is_closed:
                timing.finish("error" if response.status_code >= 400 else "success")
            else:
                response.stream = _TimedSyncStream(response.stream, timing)
            return response
        except BaseException as error:
            timing.finish("error", error)
            raise


class ObservedAsyncHttpClient(DefaultAsyncHttpxClient):
    def __init__(self, *, performance_identity=None, **kwargs):
        self.performance_identity = performance_identity
        hooks = {name: list(values) for name, values in kwargs.pop("event_hooks", {}).items()}
        hooks["response"] = [self._headers, *hooks.get("response", [])]
        kwargs["event_hooks"] = hooks
        super().__init__(**kwargs)

    @staticmethod
    async def _headers(response):
        ObservedHttpClient._headers(response)

    async def send(self, request, *args, **kwargs):
        import asyncio
        timing = RequestTiming(request, self.performance_identity)
        request.extensions["focus_request_timing"] = timing
        try:
            response = await super().send(request, *args, **kwargs)
            if response.is_closed:
                timing.finish("error" if response.status_code >= 400 else "success")
            else:
                response.stream = _TimedAsyncStream(response.stream, timing)
            return response
        except BaseException as error:
            timing.finish("cancelled" if isinstance(error, asyncio.CancelledError) else "error", error)
            raise


class ProxyFirstHttpClient(ObservedHttpClient):
    """先使用环境代理；传输失败且尚未返回响应时改走直连。"""

    def __init__(
        self,
        *,
        proxy_transport: Any | None = None,
        direct_transport: Any | None = None,
        **kwargs: Any,
    ) -> None:
        primary_kwargs = dict(kwargs)
        primary_kwargs["trust_env"] = True
        if proxy_transport is not None:
            primary_kwargs["transport"] = proxy_transport
        super().__init__(**primary_kwargs)

        direct_kwargs = dict(kwargs)
        direct_kwargs["trust_env"] = False
        if direct_transport is not None:
            direct_kwargs["transport"] = direct_transport
        self._direct_client = ObservedHttpClient(**direct_kwargs)

    def send(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        content = request.read()
        try:
            return super().send(_copy_request(request, content), *args, **kwargs)
        except _SDK_HTTPX.TransportError as exc:
            logger.warning(
                "模型代理连接失败，自动回退直连: host=%s error=%s",
                request.url.host,
                type(exc).__name__,
            )
            return self._direct_client.send(_copy_request(request, content), *args, **kwargs)

    def close(self) -> None:
        try:
            self._direct_client.close()
        finally:
            super().close()


class ProxyFirstAsyncHttpClient(ObservedAsyncHttpClient):
    """ProxyFirstHttpClient 的异步版本，用于 Agent 的流式模型调用。"""

    def __init__(
        self,
        *,
        proxy_transport: Any | None = None,
        direct_transport: Any | None = None,
        **kwargs: Any,
    ) -> None:
        primary_kwargs = dict(kwargs)
        primary_kwargs["trust_env"] = True
        if proxy_transport is not None:
            primary_kwargs["transport"] = proxy_transport
        super().__init__(**primary_kwargs)

        direct_kwargs = dict(kwargs)
        direct_kwargs["trust_env"] = False
        if direct_transport is not None:
            direct_kwargs["transport"] = direct_transport
        self._direct_client = ObservedAsyncHttpClient(**direct_kwargs)

    async def send(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        content = await request.aread()
        try:
            return await super().send(_copy_request(request, content), *args, **kwargs)
        except _SDK_HTTPX.TransportError as exc:
            logger.warning(
                "模型代理连接失败，自动回退直连: host=%s error=%s",
                request.url.host,
                type(exc).__name__,
            )
            return await self._direct_client.send(_copy_request(request, content), *args, **kwargs)

    async def aclose(self) -> None:
        try:
            await self._direct_client.aclose()
        finally:
            await super().aclose()
