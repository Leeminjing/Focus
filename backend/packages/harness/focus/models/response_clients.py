"""本文件对外提供 responses_clients 的延迟 SDK 资源端口。

输入为明确 SDK 连接选项和可选调用方 HTTP 客户端；输出为同步/异步 Responses 与 close 端口。
具体工作流为配置阶段仅保存工厂，首次实际请求创建所需客户端，保留代理优先策略；关闭未使用端口不加载 TLS。
同步与异步资源独立拥有，关闭后不得隐式重建。应用可通过 response_client_session 跨模型复用 SDK/HTTP 连接；
缓存只保存配置摘要，不记录明文凭据。配置变更淘汰旧条目，延迟到会话退出关闭，以保护在途请求。
示例：sync, async_client = responses_clients(options)；await async_client.responses.create(**payload)。
"""

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from threading import RLock

from openai import AsyncOpenAI, OpenAI

from focus.models.http_clients import ProxyFirstAsyncHttpClient, ProxyFirstHttpClient, environment_proxy_configured


class ResponseClientPool:
    """一个应用、一个事件循环的连接所有者；不保存消息、schema 或 usage。"""

    def __init__(self):
        self._loop = asyncio.get_running_loop()
        self._clients = {}
        self._slots = {}
        self._retired = []
        self._closed = False
        self._lock = RLock()

    def __deepcopy__(self, memo):
        # 设置页复制配置进行验证；连接所有权仍归同一个应用，不能复制锁/事件循环。
        return self

    def get(self, *, provider, scope, options, asynchronous, factory):
        if asynchronous and asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Responses 客户端不可跨事件循环复用")
        # 包括 NO_PROXY 和 TLS 环境：即使代理开关不变，路由也可能发生变化。
        network = {key: os.environ.get(key) for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
            "SSL_CERT_FILE", "SSL_CERT_DIR",
            "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_CUSTOM_HEADERS",
            "OPENAI_ADMIN_KEY", "OPENAI_WEBHOOK_SECRET",
        )}
        digest = hashlib.sha256(json.dumps(
            {"options": options, "network": network}, sort_keys=True,
        ).encode()).digest()
        key = (provider, digest, asynchronous)
        slot = (scope, asynchronous)
        with self._lock:
            if self._closed:
                raise RuntimeError("Responses 客户端会话已关闭")
            previous = self._slots.get(slot)
            self._slots[slot] = key
            if previous is not None and previous != key and previous not in self._slots.values():
                client = self._clients.pop(previous, None)
                if client is not None:
                    self._retired.append((previous[-1], client))
            if key not in self._clients:
                self._clients[key] = factory()
            return self._clients[key]

    async def aclose(self):
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Responses 客户端必须在所属事件循环关闭")
        with self._lock:
            self._closed = True
            clients = [(key[-1], client) for key, client in self._clients.items()] + self._retired
            self._clients.clear()
            self._slots.clear()
            self._retired = []
        errors = []
        for asynchronous, client in clients:
            try:
                if asynchronous:
                    await client.close()
                else:
                    client.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Responses 客户端清理失败", errors)


@asynccontextmanager
async def response_client_session(app_config):
    """组合根绑定共享连接；模型预算预览仍不构造 SDK/TLS。"""
    if app_config._response_client_pool is not None:
        raise RuntimeError("AppConfig 已绑定 Responses 客户端会话")
    pool = ResponseClientPool()
    app_config._response_client_pool = pool
    try:
        yield pool
    finally:
        app_config._response_client_pool = None
        await pool.aclose()


class _ClientOwner:
    def __init__(self, factory, pool=None, pool_options=None):
        self._factory = factory
        self._client = None
        self._closed = False
        self._lock = RLock()
        self._pool = pool
        self._pool_options = pool_options

    def _get(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Responses 客户端已关闭")
            if self._pool is not None:
                return self._pool.get(factory=self._factory, **self._pool_options)
            if self._client is None:
                self._client = self._factory()
            return self._client

    def _detach(self):
        with self._lock:
            self._closed = True
            client, self._client = self._client, None
            return client

    @property
    def responses(self):
        return self._get().responses


class _SyncClient(_ClientOwner):
    def close(self):
        client = self._detach()
        if client is not None:
            client.close()


class _AsyncClient(_ClientOwner):
    async def close(self):
        client = self._detach()
        if client is not None:
            await client.close()


def responses_clients(options, http_client=None, http_async_client=None, *, pool=None, provider=None, scope=None):
    # 调用方显式注入的 transport 继续独占，禁止混入共享池。
    custom = http_client is not None or http_async_client is not None
    if custom:
        pool = None

    def sync():
        proxy = not custom and environment_proxy_configured()
        return OpenAI(**options, http_client=ProxyFirstHttpClient() if proxy else http_client)

    def asynchronous():
        proxy = not custom and environment_proxy_configured()
        return AsyncOpenAI(**options, http_client=ProxyFirstAsyncHttpClient() if proxy else http_async_client)

    identity = {"provider": provider, "scope": scope, "options": dict(options)}
    return (_SyncClient(sync, pool, {**identity, "asynchronous": False}),
            _AsyncClient(asynchronous, pool, {**identity, "asynchronous": True}))
