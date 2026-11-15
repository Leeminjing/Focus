r"""本文件对外提供 ContextRunPool。

输入为 Kernel 已授权的 LoopDirective、可选 Loop scope、活动 delegation/budget、Context Run 容量和 dispatcher；输出为独立于
Patrol/Curator 的有界 Run 启动任务及持久 queued_reason。具体工作流为串行计算全部活动 Loop 的 ready/running round，
扣除正在执行 Run 和按 Run 数量预留的派发容量，在全局与每 Loop 上限内交付已授权波次；运行中的同轮允许补位，
资源不足只更新排队原因，关闭时取消派发并释放预留，Round 收口仍由 Coordinator 负责。
Loop scope 是唤醒提示；全局候选按用户优先、有界后台保留和同类 FIFO 调度，不由 Supervisor 抵达顺序决定。
示例：`await pool.drain()`。
"""

from __future__ import annotations

import asyncio
from collections import deque

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.patrol.agent_loop.coordinator import CoordinatorClaim, LoopCoordinator
from backend.app.desktop.patrol.agent_loop.execution.dispatch import LoopWaveDispatcher
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopDirective, LoopRound, LoopUserIntent
from backend.app.desktop.persistence.models import DesktopRun
from backend.app.desktop.patrol.agent_loop.resources.request_admission import PriorityFairness


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
        self._wake = lambda: None
        self._fairness = PriorityFairness()
        self._waiting: dict[str, int] = {}
        self._sequence = 0

    def bind_wakeup(self, wake) -> None:
        self._wake = wake

    async def drain(self, loop_id: str | None = None) -> int:
        async with self._drain_lock:
            return await self._drain()

    async def _drain(self) -> int:
        self._reap()
        active = await self._active_run_counts()
        reserved: dict[str, int] = {}
        for reserved_loop, slots, _ in self._tasks.values():
            reserved[reserved_loop] = reserved.get(reserved_loop, 0) + slots
        capacity = self._concurrency - sum(active.values()) - sum(reserved.values())
        if capacity <= 0:
            await self._mark_all_waiting("global_context_capacity")
            return 0
        # Every scoped wake competes against the same durable global queue.
        candidates = await self._candidates()
        identities = {row[1] for row in candidates}
        self._waiting = {key: age for key, age in self._waiting.items() if key in identities}
        for _, round_id, _, _, _ in candidates:
            if round_id not in self._waiting:
                self._waiting[round_id] = self._sequence
                self._sequence += 1
        eligible = []
        for candidate_loop, round_id, per_loop_limit, queued, priority in candidates:
            if per_loop_limit > active.get(candidate_loop, 0) + reserved.get(candidate_loop, 0):
                eligible.append((candidate_loop, round_id, per_loop_limit, queued, priority))
            else:
                await self._set_reason(round_id, "context_capacity")
        eligible.sort(key=lambda row: self._waiting[row[1]])
        queues = (deque(row for row in eligible if not row[4]), deque(row for row in eligible if row[4]))
        started = 0
        while queues[0] or queues[1]:
            priority = self._fairness.choose_priority(bool(queues[1]), bool(queues[0]))
            loop_id, round_id, per_loop_limit, queued, _ = queues[int(priority)].popleft()
            # Share a wave with other eligible rounds before allowing a large
            # single Round to reserve the entire newly available capacity.
            slots = min(capacity, per_loop_limit - active.get(loop_id, 0) - reserved.get(loop_id, 0), queued,
                        max(1, capacity // (len(queues[0]) + len(queues[1]) + 1)))
            if slots <= 0:
                await self._set_reason(round_id, "global_context_capacity" if capacity <= 0 else "context_capacity")
                continue
            task = asyncio.create_task(
                self._launch(loop_id, round_id, slots),
                name=f"loop-context-run:{loop_id}:{round_id}",
            )
            self._tasks[round_id] = (loop_id, slots, task)
            reserved[loop_id] = reserved.get(loop_id, 0) + slots
            task.add_done_callback(self._launch_done)
            self._fairness.admitted(priority)
            self._waiting[round_id] = self._sequence
            self._sequence += 1
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

    async def _candidates(self) -> tuple[tuple[str, str, int, int, bool], ...]:
        async with self._sessions() as session:
            priority = select(LoopUserIntent.intent_id).where(
                LoopUserIntent.loop_id == AgentLoop.loop_id,
                LoopUserIntent.observed_round_id == LoopRound.round_id,
                LoopUserIntent.status.in_(("pending", "observed")),
            ).exists()
            statement = (
                select(AgentLoop.loop_id, LoopRound.round_id, LoopDelegationGrant.budgets, func.count(LoopDirective.directive_id), priority)
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
                .where(AgentLoop.status == "running", LoopRound.decision_id.is_not(None),
                       LoopRound.status.in_(("ready", "running")), LoopRound.round_id.not_in(tuple(self._tasks)))
                .group_by(AgentLoop.loop_id, LoopRound.round_id, LoopDelegationGrant.budgets, LoopRound.started_at)
                .order_by(LoopRound.started_at, LoopRound.round_id)
            )
            rows = list(
                (
                    await session.execute(statement)
                ).all()
            )
            return tuple(
                (str(loop_id), str(round_id), max(1, int((budgets or {}).get("max_concurrent_runs") or self._concurrency)), int(queued), bool(priority))
                for loop_id, round_id, budgets, queued, priority in rows
                if str(round_id) not in self._tasks
            )

    def _launch_done(self, task: asyncio.Task) -> None:
        """Only progress wakes the pool; expired authority must not hot-loop."""
        if not task.cancelled() and task.exception() is None and task.result():
            self._wake()

    async def _launch(self, loop_id: str, round_id: str, capacity: int) -> bool:
        claim = CoordinatorClaim("context-pool", loop_id, round_id, "0")
        try:
            run_ids = await self._coordinator.dispatch_ready(claim, self._dispatcher, capacity)
            return bool(run_ids)
        except asyncio.CancelledError:
            await self._set_reason(round_id, "component_stopped")
            raise
        except Exception as exc:
            await self._set_reason(round_id, f"launch_retry:{str(exc)[:500]}")
            return False

    async def _active_run_count(self, loop_id: str | None = None) -> int:
        counts = await self._active_run_counts()
        return sum(counts.values()) if loop_id is None else counts.get(loop_id, 0)

    async def _active_run_counts(self) -> dict[str, int]:
        async with self._sessions() as session:
            query = select(DesktopRun.loop_id, func.count()).where(
                DesktopRun.loop_id.is_not(None),
                DesktopRun.status.in_(("pending", "running")),
            ).group_by(DesktopRun.loop_id)
            return {loop_id: int(count) for loop_id, count in await session.execute(query)}

    async def _set_reason(self, round_id: str, reason: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(LoopDirective)
                .where(LoopDirective.round_id == round_id, LoopDirective.status == "created", LoopDirective.lifecycle_state == "authorized")
                .values(queued_reason=reason[:1000])
            )

    async def _mark_all_waiting(self, reason: str) -> None:
        async with self._sessions.begin() as session:
            statement = (
                update(LoopDirective)
                .where(
                    LoopDirective.status == "created",
                    LoopDirective.lifecycle_state == "authorized",
                    LoopDirective.loop_id.in_(select(AgentLoop.loop_id).where(AgentLoop.status == "running")),
                )
            )
            await session.execute(statement.values(queued_reason=reason))

    def _reap(self) -> None:
        completed = tuple(round_id for round_id, (_, _, task) in self._tasks.items() if task.done())
        for round_id in completed:
            _, _, task = self._tasks.pop(round_id)
            if not task.cancelled():
                task.exception()
