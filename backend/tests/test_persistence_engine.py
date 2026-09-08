"""
本文件验证进程级 SQLAlchemy AsyncEngine 的关闭契约。

输入为注入 persistence.engine 模块的可观察假引擎与 session factory；输出为连接池释放
调用和全局引用状态。测试工作流先建立全局资源，再等待 dispose_engine，最后断言异步
dispose 被完整执行且后续 session 获取被拒绝。
"""

from __future__ import annotations

import asyncio

import pytest

from focus.persistence import engine as engine_module


class _ObservedAsyncEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def test_dispose_engine_awaits_pool_shutdown_and_clears_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _ObservedAsyncEngine()
    monkeypatch.setattr(engine_module, "_engine", observed)
    monkeypatch.setattr(engine_module, "_session_factory", object())

    asyncio.run(engine_module.dispose_engine())

    assert observed.disposed is True
    assert engine_module._engine is None
    assert engine_module._session_factory is None
    with pytest.raises(RuntimeError, match="session factory"):
        engine_module.get_session()
