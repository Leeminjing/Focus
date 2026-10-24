"""Opt-in process and SQL totals; never captures SQL text, parameters or content."""

import asyncio
from collections import defaultdict
import os
import logging
from time import perf_counter

import psutil
from sqlalchemy import event

from focus.models.telemetry import emit, operation_context


class DesktopPerformanceMonitor:
    def __init__(self, engine, interval=60):
        self.engine = engine.sync_engine
        self.interval = interval
        self._totals = defaultdict(lambda: [0, 0.0, 0])
        self._task = None

    def start(self):
        if self._task is not None:
            return
        self._logger = logging.getLogger("focus.performance")
        self._previous_level = self._logger.level
        self._handler = None
        if not self._logger.hasHandlers():
            self._handler = logging.StreamHandler()
            self._handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.INFO)
        self._process = psutil.Process()
        self._process.cpu_percent()
        self._sampled_at = perf_counter()
        event.listen(self.engine, "before_cursor_execute", self._before)
        event.listen(self.engine, "after_cursor_execute", self._after)
        event.listen(self.engine, "handle_error", self._error)
        self._task = asyncio.create_task(self._sample(), name="desktop-performance")

    def _before(self, _conn, _cursor, _statement, _parameters, context, _many):
        context._focus_performance = (perf_counter(), operation_context.get())

    def _record(self, context, failed):
        timing = getattr(context, "_focus_performance", None)
        if timing is not None:
            started, category = timing
            total = self._totals[category]
            total[0] += 1
            total[1] += (perf_counter() - started) * 1000
            total[2] += int(failed)
            del context._focus_performance

    def _after(self, _conn, _cursor, _statement, _parameters, context, _many):
        self._record(context, False)

    def _error(self, error):
        if error.execution_context is not None:
            self._record(error.execution_context, True)

    async def _sample(self):
        while True:
            await asyncio.sleep(self.interval)
            await self.sample()

    async def sample(self):
        totals, self._totals = self._totals, defaultdict(lambda: [0, 0.0, 0])
        now = perf_counter()
        elapsed, self._sampled_at = now - self._sampled_at, now
        memory, cpu = await asyncio.to_thread(lambda: (self._process.memory_info().rss, self._process.cpu_percent()))
        emit("process_sample", pid=os.getpid(), process="backend", rss_bytes=memory,
             cpu_percent=cpu, interval_seconds=elapsed,
             sql={key: {"queries": value[0], "duration_ms": value[1], "errors": value[2]}
                  for key, value in totals.items()})

    async def close(self):
        if self._task is None:
            return
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
            await self.sample()
        event.remove(self.engine, "before_cursor_execute", self._before)
        event.remove(self.engine, "after_cursor_execute", self._after)
        event.remove(self.engine, "handle_error", self._error)
        if self._handler is not None:
            self._logger.removeHandler(self._handler)
            self._handler.close()
        self._logger.setLevel(self._previous_level)
