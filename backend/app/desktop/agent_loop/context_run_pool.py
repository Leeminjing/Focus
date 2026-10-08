r"""本文件对外提供 ContextRunPool。

输入为 Kernel 已授权的 LoopDirective、可选 Loop scope、活动 delegation/budget、Context Run 容量和 dispatcher；输出为独立于
Patrol/Curator 的有界 Run 启动任务及持久 queued_reason。具体工作流为串行计算当前 Loop 的 ready/running round，
扣除活动 Run 和按 Run 数量预留的派发容量，在全局与每 Loop 上限内交付已授权波次；运行中的同轮允许补位，
资源不足只更新排队原因，关闭时取消派发并释放预留，Round 收口仍由 Coordinator 负责。
示例：`await pool.drain()`。
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim, LoopCoordinator
from backend.app.desktop.agent_loop.dispatch import LoopWaveDispatcher
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopDirective, LoopRound
from backend.app.desktop.models import DesktopRun


class ContextRunPool:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        coordinator: LoopCoordinator,
        dispatcher: LoopWaveDispatcher,
        *,
        concurrency: int = 4,
    ) -> None:
        self._sessions = sessions
        self._coordinator = coordinator
        self._dispatcher = dispatcher
        self._concurrency = max(1, concurrency)
        self._tasks: dict[str, tuple[str, int, asyncio.Task]] = {}
        self._drain_lock = asyncio.Lock()

    async def drain(self, loop_id: str | None = None) -> int:
        async with self._drain_lock:
            return await self._drain(loop_id)

    async def _drain(self, loop_id: str | None) -> int:
        self._reap()
        global_active = await self._active_run_count()
        capacity = self._concurrency - global_active - sum(slots for _, slots, _ in self._tasks.values())
        if capacity <= 0:
            await self._mark_all_waiting("global_context_capacity", loop_id)
            return 0
        candidates = await self._candidates(loop_id)
        started = 0
        for loop_id, round_id, per_loop_limit, queued in candidates:
            active_for_loop = await self._active_run_count(loop_id)
            reserved = sum(slots for reserved_loop, slots, _ in self._tasks.values() if reserved_loop == loop_id)
            slots = min(capacity, per_loop_limit - active_for_loop - reserved, queued)
            if slots <= 0:
                await self._set_reason(round_id, "global_context_capacity" if capacity <= 0 else "context_capacity")
                continue
            task = asyncio.create_task(
                self._launch(loop_id, round_id, slots),
                name=f"loop-context-run:{loop_id}:{round_id}",
            )
            self._tasks[round_id] = (loop_id, slots, task)
            capacity -= slots
            started += 1
        return started

    async def close(self) -> None:
        tasks = tuple(task for _, _, task in self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _candidates(self, loop_id: str | None = None) -> tuple[tuple[str, str, int, int], ...]:
        async with self._sessions() as session:
            statement = (
                select(AgentLoop.loop_id, LoopRound.round_id, LoopDelegationGrant.budgets, func.count(LoopDirective.directive_id))
                .join(LoopRound, LoopRound.loop_id == AgentLoop.loop_id)
                .join(
                    LoopDelegationGrant,
                    (LoopDelegationGrant.loop_id == AgentLoop.loop_id)
                    & (LoopDelegationGrant.revision == AgentLoop.authority_revision)
                    & (LoopDelegationGrant.status == "active"),
                )
                .join(
                    LoopDirective,
                    (LoopDirective.round_id == LoopRound.round_id)
                    & (LoopDirective.status == "created")
                    & (LoopDirective.lifecycle_state == "authorized")
                    & (LoopDirective.attempt < LoopDirective.max_attempts),
                )
                .where(AgentLoop.status == "running", AgentLoop.current_round_id == LoopRound.round_id,
                       LoopRound.status.in_(("ready", "running")), LoopRound.round_id.not_in(tuple(self._tasks)))
                .group_by(AgentLoop.loop_id, LoopRound.round_id, LoopDelegationGrant.budgets, LoopRound.started_at)
                .order_by(LoopRound.started_at, LoopRound.round_id)
            )
            if loop_id is not None:
                statement = statement.where(AgentLoop.loop_id == loop_id)
            rows = list(
                (
                    await session.execute(statement)
                ).all()
            )
            return tuple(
                (str(loop_id), str(round_id), max(1, int((budgets or {}).get("max_concurrent_runs") or self._concurrency)), int(queued))
                for loop_id, round_id, budgets, queued in rows
                if str(round_id) not in self._tasks
            )

    async def _launch(self, loop_id: str, round_id: str, capacity: int) -> None:
        claim = CoordinatorClaim("context-pool", loop_id, round_id, "0")
        try:
            await self._coordinator.dispatch_ready(claim, self._dispatcher, capacity)
        except asyncio.CancelledError:
            await self._set_reason(round_id, "component_stopped")
            raise
        except Exception as exc:
            await self._set_reason(round_id, f"launch_retry:{str(exc)[:500]}")

    async def _active_run_count(self, loop_id: str | None = None) -> int:
        async with self._sessions() as session:
            query = select(func.count()).select_from(DesktopRun).where(
                DesktopRun.loop_id.is_not(None), DesktopRun.status.in_(("pending", "running"))
            )
            if loop_id is not None:
                query = query.where(DesktopRun.loop_id == loop_id)
            return int(await session.scalar(query) or 0)

    async def _set_reason(self, round_id: str, reason: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(LoopDirective)
                .where(LoopDirective.round_id == round_id, LoopDirective.status == "created", LoopDirective.lifecycle_state == "authorized")
                .values(queued_reason=reason[:1000])
            )

    async def _mark_all_waiting(self, reason: str, loop_id: str | None = None) -> None:
        async with self._sessions.begin() as session:
            statement = (
                update(LoopDirective)
                .where(
                    LoopDirective.status == "created",
                    LoopDirective.lifecycle_state == "authorized",
                    LoopDirective.loop_id.in_(select(AgentLoop.loop_id).where(AgentLoop.status == "running")),
                )
            )
            if loop_id is not None:
                statement = statement.where(LoopDirective.loop_id == loop_id)
            await session.execute(statement.values(queued_reason=reason))

    def _reap(self) -> None:
        completed = tuple(round_id for round_id, (_, _, task) in self._tasks.items() if task.done())
        for round_id in completed:
            _, _, task = self._tasks.pop(round_id)
            if not task.cancelled():
                task.exception()
