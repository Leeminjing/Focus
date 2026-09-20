r"""本文件对外提供 LoopSupervisorRegistry。

输入为活动 Loop identity 集合和按 Loop 构造监督组件的工厂；输出为与活动集合严格一致的 per-Loop Supervisor 生命周期。
具体工作流为 reconcile 创建新 Loop 的独立 Supervisor、关闭已离开运行态的 Supervisor，close 统一等待全部组件收敛。
示例：`await registry.reconcile(("loop-a", "loop-b"))`。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from backend.app.desktop.agent_loop.supervisor import LoopSupervisor, SupervisorComponent


class LoopSupervisorRegistry:
    def __init__(
        self,
        component_factory: Callable[[str], Iterable[SupervisorComponent]],
    ) -> None:
        self._component_factory = component_factory
        self._supervisors: dict[str, LoopSupervisor] = {}

    @property
    def loop_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._supervisors))

    async def reconcile(self, loop_ids: Iterable[str]) -> tuple[str, ...]:
        desired = frozenset(loop_ids)
        removed = tuple(sorted(set(self._supervisors) - desired))
        for loop_id in removed:
            supervisor = self._supervisors.pop(loop_id)
            await supervisor.close()
        added = tuple(sorted(desired - set(self._supervisors)))
        for loop_id in added:
            supervisor = LoopSupervisor(f"agent-loop:{loop_id}", self._component_factory(loop_id))
            self._supervisors[loop_id] = supervisor
            await supervisor.start()
        return added

    async def close(self) -> None:
        supervisors = tuple(self._supervisors.values())
        self._supervisors.clear()
        for supervisor in supervisors:
            await supervisor.close()
