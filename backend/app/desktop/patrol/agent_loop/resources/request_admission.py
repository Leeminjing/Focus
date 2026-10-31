"""请求级有界优先准入；后台等待时，每三次用户请求后为其保留一次机会。

只持有实际请求的额度，调用者在离开 slot 后执行模型清理和消费结算。
FIFO 保持同类公平，取消等待者或刚获准者均不会泄漏额度。
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager


class PriorityFairness:
    def __init__(self, priority_burst: int = 3) -> None:
        self._limit = max(1, priority_burst)
        self._streak = 0

    def choose_priority(self, priority_waiting: bool, background_waiting: bool) -> bool:
        return priority_waiting and (not background_waiting or self._streak < self._limit)

    def admitted(self, priority: bool) -> None:
        self._streak = min(self._limit, self._streak + 1) if priority else 0


class RequestAdmission:
    def __init__(self, concurrency: int, *, priority_burst: int = 3) -> None:
        self._available = max(1, concurrency)
        self._queues: tuple[deque[asyncio.Future], deque[asyncio.Future]] = (deque(), deque())
        self._fairness = PriorityFairness(priority_burst)

    @asynccontextmanager
    async def slot(self, *, priority: bool = False):
        waiter = asyncio.get_running_loop().create_future()
        queue = self._queues[int(priority)]
        queue.append(waiter)
        self._dispatch()
        try:
            await waiter
        except BaseException:
            if waiter.cancelled():
                if waiter in queue:
                    queue.remove(waiter)
            else:
                self._available += 1
            self._dispatch()
            raise
        try:
            yield
        finally:
            self._available += 1
            self._dispatch()

    def _dispatch(self) -> None:
        background, priority = self._queues
        while self._available and (background or priority):
            urgent = self._fairness.choose_priority(bool(priority), bool(background))
            waiter = self._queues[int(urgent)].popleft()
            # Task cancellation can precede its coroutine's exception handler.
            if waiter.cancelled():
                continue
            self._fairness.admitted(urgent)
            self._available -= 1
            waiter.set_result(None)
