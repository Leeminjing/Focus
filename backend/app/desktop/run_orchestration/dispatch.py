r"""本文件对外提供 RunDispatchRepository、RunDispatchRecovery 与 DurableRunDispatchWorker。

输入为 accepted/遗留 dispatch、worker identity、租约、fencing token、执行装配器与启动函数；输出为有界领取、running/settled、显式启动失败或重启分类状态。
具体工作流为 repository 使用 skip-locked 领取并递增 fencing，worker 在租约内装配和启动；recovery 只把带完整 durable Main 执行快照且可证明未启动的工作恢复为 accepted，并把旧 Patrol 或不确定执行标为 interrupted；后续转换必须携带当前 token，旧 owner 无法提交。
repository 另有按 Run 定向认领入口，使用同一状态列与 fencing 语义，使已有启动者的 Run（例如 directive 启动端口负责的 Run）不会被队列消费者重复认领。
示例：`processed = await worker.drain(limit=4)`。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.run_orchestration.assembler import RunExecutionAssembler, RunExecutionAssembly
from backend.app.desktop.run_orchestration.models import RunDispatch
from backend.app.desktop.models import DesktopRun


class StaleDispatchFence(RuntimeError):
    pass


class RunDispatchRepository:
    async def claim(
        self,
        session: AsyncSession,
        worker_id: str,
        *,
        lease_seconds: int = 60,
    ) -> RunDispatch | None:
        row = await session.scalar(
            select(RunDispatch).where(
                self._claimable()
            ).order_by(RunDispatch.accepted_at, RunDispatch.dispatch_id).with_for_update(skip_locked=True).limit(1)
        )
        if row is None:
            return None
        return await self._mark_claimed(session, row, worker_id, lease_seconds=lease_seconds)

    async def claim_run(
        self,
        session: AsyncSession,
        run_id: str,
        owner_id: str,
        *,
        lease_seconds: int = 60,
    ) -> RunDispatch | None:
        """按 Run 定向认领：让该 Run 的启动者成为唯一持有者，语义与队列认领一致（同一状态列与 fencing token）。"""

        row = await session.scalar(
            select(RunDispatch).where(
                RunDispatch.run_id == run_id,
                self._claimable(),
            ).with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        return await self._mark_claimed(session, row, owner_id, lease_seconds=lease_seconds)

    @staticmethod
    def _claimable() -> Any:
        return or_(
            RunDispatch.status == "accepted",
            (RunDispatch.status == "claimed") & (RunDispatch.lease_expires_at < datetime.now(UTC)),
        )

    @staticmethod
    async def _mark_claimed(
        session: AsyncSession,
        row: RunDispatch,
        owner_id: str,
        *,
        lease_seconds: int,
    ) -> RunDispatch:
        now = datetime.now(UTC)
        row.status = "claimed"
        row.claimed_by = owner_id
        row.attempt += 1
        row.fencing_token += 1
        row.claimed_at = now
        row.lease_expires_at = now + timedelta(seconds=max(10, lease_seconds))
        row.error = None
        await session.flush()
        return row

    async def by_run(self, session: AsyncSession, run_id: str) -> RunDispatch | None:
        return await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id))

    async def renew(
        self,
        session: AsyncSession,
        dispatch_id: str,
        fencing_token: int,
        worker_id: str,
        *,
        lease_seconds: int = 60,
    ) -> RunDispatch:
        row = await session.get(RunDispatch, dispatch_id, with_for_update=True)
        if row is None or row.fencing_token != fencing_token or row.claimed_by != worker_id or row.status != "claimed":
            raise StaleDispatchFence(dispatch_id)
        row.lease_expires_at = datetime.now(UTC) + timedelta(seconds=max(10, lease_seconds))
        await session.flush()
        return row

    async def settle_by_run(self, session: AsyncSession, run_id: str) -> RunDispatch | None:
        row = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id).with_for_update())
        if row is None or row.status in {"settled", "failed_to_start", "interrupted"}:
            return row
        row.status = "settled"
        row.settled_at = datetime.now(UTC)
        row.lease_expires_at = None
        await session.flush()
        return row

    async def transition(
        self,
        session: AsyncSession,
        dispatch_id: str,
        fencing_token: int,
        target: str,
        *,
        error: str | None = None,
    ) -> RunDispatch:
        row = await session.get(RunDispatch, dispatch_id, with_for_update=True)
        if row is None:
            raise LookupError(dispatch_id)
        if row.fencing_token != fencing_token:
            raise StaleDispatchFence(dispatch_id)
        allowed = {
            "claimed": {"running", "failed_to_start", "interrupted"},
            "running": {"settled", "failed_to_start", "interrupted"},
        }
        if target not in allowed.get(row.status, set()):
            raise ValueError(f"非法 dispatch 转换: {row.status} -> {target}")
        row.status = target
        row.error = error
        now = datetime.now(UTC)
        run = await session.get(DesktopRun, row.run_id, with_for_update=True)
        if target == "running":
            row.running_at = now
            if run is not None and run.status == "pending":
                run.status = "running"
        if target in {"settled", "interrupted", "failed_to_start"}:
            row.settled_at = now
            row.lease_expires_at = None
        if run is not None and target in {"interrupted", "failed_to_start"}:
            run.status = "interrupted" if target == "interrupted" else "error"
            run.error = error
            run.settled_at = now
        await session.flush()
        return row


@dataclass(frozen=True, slots=True)
class RunDispatchRecoveryReport:
    safe_run_ids: tuple[str, ...]
    interrupted_run_ids: tuple[str, ...]


class RunDispatchRecovery:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def reconcile(self) -> RunDispatchRecoveryReport:
        now = datetime.now(UTC)
        safe: list[str] = []
        interrupted: list[str] = []
        async with self._sessions.begin() as session:
            runs = tuple((await session.scalars(select(DesktopRun).where(DesktopRun.status.in_(("pending", "running"))).order_by(DesktopRun.run_id).with_for_update())).all())
            for run in runs:
                dispatch = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run.run_id).with_for_update())
                restart_safe = self._restart_safe(run)
                if dispatch is None:
                    dispatch = RunDispatch(
                        dispatch_id=uuid.uuid5(uuid.NAMESPACE_URL, f"focus:run-dispatch:{run.run_id}").hex,
                        run_id=run.run_id,
                        status="accepted" if restart_safe else "interrupted",
                        error=None if restart_safe else "process_restarted_with_uncertain_execution",
                        settled_at=None if restart_safe else now,
                    )
                    session.add(dispatch)
                if restart_safe and dispatch.status in {"accepted", "claimed"}:
                    dispatch.status = "accepted"
                    dispatch.claimed_by = None
                    dispatch.lease_expires_at = None
                    dispatch.error = None
                    safe.append(run.run_id)
                    continue
                dispatch.status = "interrupted"
                dispatch.error = "process_restarted_with_uncertain_execution"
                dispatch.settled_at = now
                dispatch.lease_expires_at = None
                interrupted.append(run.run_id)
        return RunDispatchRecoveryReport(tuple(safe), tuple(interrupted))

    @staticmethod
    def _restart_safe(run: DesktopRun) -> bool:
        execution = (run.equipment or {}).get("_durable_dispatch_execution")
        return run.kind == "main" and run.status == "pending" and isinstance(execution, dict) and execution.get("agent_role") == "main"

RunAssemblyStarter = Callable[[RunExecutionAssembly], Awaitable[Any]]
RunStartedObserver = Callable[[Any], None]
RunStartFailureObserver = Callable[[str, str], Awaitable[None]]


class DurableRunDispatchWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        worker_id: str,
        assembler: RunExecutionAssembler,
        starter: RunAssemblyStarter,
        repository: RunDispatchRepository | None = None,
        startup_deadline_seconds: int = 60,
        on_started: RunStartedObserver | None = None,
        on_failed: RunStartFailureObserver | None = None,
    ) -> None:
        self._sessions = sessions
        self._worker_id = worker_id
        self._assembler = assembler
        self._starter = starter
        self._repository = repository or RunDispatchRepository()
        self._startup_deadline_seconds = max(10, startup_deadline_seconds)
        self._on_started = on_started
        self._on_failed = on_failed

    async def drain(self, *, limit: int = 4) -> int:
        processed = 0
        for _ in range(max(1, limit)):
            async with self._sessions.begin() as session:
                dispatch = await self._repository.claim(session, self._worker_id)
                if dispatch is None:
                    break
                identity = (dispatch.dispatch_id, dispatch.run_id, dispatch.fencing_token)
            try:
                assembly = await self._assemble_with_lease(identity)
                async with self._sessions.begin() as session:
                    await self._repository.transition(session, identity[0], identity[2], "running")
                started = await asyncio.wait_for(
                    self._starter(assembly),
                    timeout=self._startup_deadline_seconds,
                )
            except StaleDispatchFence:
                processed += 1
                continue
            except Exception as error:
                if self._on_failed is not None:
                    await self._on_failed(identity[1], str(error))
                async with self._sessions.begin() as session:
                    await self._repository.transition(session, identity[0], identity[2], "failed_to_start", error=str(error)[:4000])
            else:
                if self._on_started is not None:
                    self._on_started(started)
            processed += 1
        return processed

    async def _assemble_with_lease(self, identity: tuple[str, str, int]) -> RunExecutionAssembly:
        async def operation() -> RunExecutionAssembly:
            return await self._assembler.assemble(identity[1])

        task = asyncio.create_task(operation())
        deadline = asyncio.get_running_loop().time() + self._startup_deadline_seconds
        interval = max(3.0, self._startup_deadline_seconds / 3)
        try:
            while not task.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("Run dispatch 启动超过 deadline")
                done, _ = await asyncio.wait((task,), timeout=min(interval, remaining))
                if done:
                    return await task
                async with self._sessions.begin() as session:
                    await self._repository.renew(
                        session,
                        identity[0],
                        identity[2],
                        self._worker_id,
                        lease_seconds=self._startup_deadline_seconds,
                    )
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
