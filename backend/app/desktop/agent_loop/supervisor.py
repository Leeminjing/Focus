r"""本文件对外提供 LoopSupervisor、SupervisorComponent 与 ComponentState。

输入为具有独立 tick 的命名组件、轮询间隔和 Loop/supervisor identity；输出为可启动、暂停、恢复、停止且故障隔离的并发组件生命周期。
具体工作流为每个组件拥有独立 asyncio task，pause 在下一次 tick 前建立屏障，单组件异常记录失败并有界退避，
stop 取消全部 task 并等待收敛。示例：`await LoopSupervisor("runtime", components).start()`。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
import logging


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SupervisorComponent:
    name: str
    tick: Callable[[], Awaitable[object]]
    poll_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class ComponentState:
    name: str
    running: bool
    failures: int
    last_error: str | None


class LoopSupervisor:
    def __init__(self, supervisor_id: str, components: Iterable[SupervisorComponent]) -> None:
        self.supervisor_id = supervisor_id
        self._components = tuple(components)
        self._tasks: dict[str, asyncio.Task] = {}
        self._states = {item.name: ComponentState(item.name, False, 0, None) for item in self._components}
        self._stop = asyncio.Event()
        self._resume = asyncio.Event()
        self._resume.set()

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._resume.set()
        self._tasks = {item.name: asyncio.create_task(self._run_component(item), name=f"{self.supervisor_id}:{item.name}") for item in self._components}

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    async def close(self) -> None:
        self._stop.set()
        self._resume.set()
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def states(self) -> tuple[ComponentState, ...]:
        return tuple(self._states[item.name] for item in self._components)

    async def _run_component(self, component: SupervisorComponent) -> None:
        failures = 0
        self._states[component.name] = ComponentState(component.name, True, 0, None)
        try:
            while not self._stop.is_set():
                await self._resume.wait()
                try:
                    await component.tick()
                    failures = 0
                    self._states[component.name] = ComponentState(component.name, True, 0, None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    self._states[component.name] = ComponentState(component.name, True, failures, str(exc)[:1000])
                    logger.exception("Loop supervisor component failed: %s", component.name)
                delay = min(max(component.poll_seconds, 0.001) * (2 ** min(failures, 5)), 5.0)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            state = self._states[component.name]
            self._states[component.name] = ComponentState(component.name, False, state.failures, state.last_error)
