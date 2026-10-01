"""本文件对外提供 responses_clients 的延迟 SDK 资源端口。

输入为明确 SDK 连接选项和可选调用方 HTTP 客户端；输出为同步/异步 Responses 与 close 端口。
具体工作流为配置阶段仅保存工厂，首次实际请求创建所需客户端，保留代理优先策略；关闭未使用端口不加载 TLS。
同步与异步资源独立拥有，关闭后不得隐式重建。凭据只传给 SDK，不参与 repr、序列化或日志。
示例：sync, async_client = responses_clients(options)；await async_client.responses.create(**payload)。
"""

from threading import RLock

from openai import AsyncOpenAI, OpenAI

from focus.models.http_clients import ProxyFirstAsyncHttpClient, ProxyFirstHttpClient, environment_proxy_configured


class _ClientOwner:
    def __init__(self, factory):
        self._factory = factory
        self._client = None
        self._closed = False
        self._lock = RLock()

    def _get(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Responses 客户端已关闭")
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


def responses_clients(options, http_client=None, http_async_client=None):
    proxy = http_client is None and http_async_client is None and environment_proxy_configured()

    def sync():
        return OpenAI(**options, http_client=ProxyFirstHttpClient() if proxy else http_client)

    def asynchronous():
        return AsyncOpenAI(**options, http_client=ProxyFirstAsyncHttpClient() if proxy else http_async_client)

    return _SyncClient(sync), _AsyncClient(asynchronous)
