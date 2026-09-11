"""OpenAI 兼容模型的代理优先、直连回退 HTTP 客户端。"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

import httpx
from openai import DefaultAsyncHttpxClient, DefaultHttpxClient


logger = logging.getLogger(__name__)

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


def _copy_request(request: httpx.Request, content: bytes) -> httpx.Request:
    """复制可重放的模型请求，保留 OpenAI 注入的头与超时扩展。"""
    return httpx.Request(
        method=request.method,
        url=request.url,
        headers=request.headers,
        content=content,
        extensions=dict(request.extensions),
    )


class ProxyFirstHttpClient(DefaultHttpxClient):
    """先使用环境代理；传输失败且尚未返回响应时改走直连。"""

    def __init__(
        self,
        *,
        proxy_transport: httpx.BaseTransport | None = None,
        direct_transport: httpx.BaseTransport | None = None,
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
        self._direct_client = DefaultHttpxClient(**direct_kwargs)

    def send(self, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        content = request.read()
        try:
            return super().send(_copy_request(request, content), *args, **kwargs)
        except httpx.TransportError as exc:
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


class ProxyFirstAsyncHttpClient(DefaultAsyncHttpxClient):
    """ProxyFirstHttpClient 的异步版本，用于 Agent 的流式模型调用。"""

    def __init__(
        self,
        *,
        proxy_transport: httpx.AsyncBaseTransport | None = None,
        direct_transport: httpx.AsyncBaseTransport | None = None,
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
        self._direct_client = DefaultAsyncHttpxClient(**direct_kwargs)

    async def send(self, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        content = await request.aread()
        try:
            return await super().send(_copy_request(request, content), *args, **kwargs)
        except httpx.TransportError as exc:
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
