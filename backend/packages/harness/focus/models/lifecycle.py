"""模型独占连接的任务所有权；Run 子任务继承同一集合，并发 Run 相互隔离。

连接工厂登记自己拥有的端口，调用方注入的连接仍由调用方回收。
应用池的借用端口可以关闭，但不会关闭池连接。未使用的 Responses 端口保持惰性。
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from threading import RLock


logger = logging.getLogger(__name__)
_OWNERS = ContextVar("model_client_owners", default=None)


class _ModelClients:
    def __init__(self):
        self._clients = {}
        self._closed = False
        self._lock = RLock()

    def retain(self, client, asynchronous):
        if client is None:
            return
        with self._lock:
            if self._closed:
                raise RuntimeError("模型连接作用域已关闭")
            self._clients.setdefault(id(client), (client, asynchronous))

    async def close(self):
        with self._lock:
            self._closed = True
            clients = list(self._clients.values())
            self._clients.clear()
        errors = []
        for client, asynchronous in reversed(clients):
            try:
                if asynchronous:
                    await client.close()
                else:
                    client.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("模型连接回收失败", errors)


def begin_model_clients():
    return _OWNERS.set(_ModelClients())


def retain_model_clients(*, sync_client=None, async_client=None):
    owner = _OWNERS.get()
    if owner is not None:
        owner.retain(sync_client, False)
        owner.retain(async_client, True)


async def end_model_clients(token):
    owner = _OWNERS.get()
    _OWNERS.reset(token)
    if owner is not None:
        # 二次取消（例如用户中断后紧接应用退出）不能打断其余连接的回收。
        cleanup = asyncio.create_task(owner.close())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError()


@asynccontextmanager
async def model_client_scope():
    """为配置验证等不经 Run 的模型调用提供独立所有权边界。"""
    token = begin_model_clients()
    try:
        yield
    finally:
        try:
            await end_model_clients(token)
        except Exception:
            logger.exception("模型连接回收失败")
