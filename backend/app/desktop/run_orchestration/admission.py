r"""本文件对外提供 RunAdmissionService 与 RunAdmissionResult。

输入为调用方事务中已构造但未提交的 DesktopRun；输出为同事务持久化的唯一 Run 与 accepted RunDispatch。
具体工作流为先取得 task 与 idempotency key 的事务级互斥锁，再查找幂等赢家、校验 task-local 活跃 Main Run，最后同时写 Run/dispatch 并 flush，不创建 Agent。
示例：`result = await service.admit(session, run)`。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.models import DesktopRun
from backend.app.desktop.persistence_safety import PersistencePayloadNormalizer
from backend.app.desktop.run_orchestration.models import RunDispatch


@dataclass(frozen=True, slots=True)
class RunAdmissionResult:
    run: DesktopRun
    dispatch: RunDispatch
    created: bool


class RunAdmissionConflict(RuntimeError):
    pass


class RunAdmissionService:
    async def admit(self, session: AsyncSession, run: DesktopRun) -> RunAdmissionResult:
        await session.execute(select(func.pg_advisory_xact_lock(self._lock_key("task", run.task_id))))
        if run.idempotency_key:
            await session.execute(select(func.pg_advisory_xact_lock(self._lock_key("idempotency", run.idempotency_key))))
            existing = await session.scalar(
                select(DesktopRun).where(DesktopRun.idempotency_key == run.idempotency_key).with_for_update()
            )
            if existing is not None:
                dispatch = await self._dispatch(session, existing.run_id)
                return RunAdmissionResult(existing, dispatch, False)
        active = await session.scalar(
            select(DesktopRun).where(
                DesktopRun.task_id == run.task_id,
                DesktopRun.kind == "main",
                DesktopRun.status.in_(("pending", "running")),
                DesktopRun.run_id != run.run_id,
            ).with_for_update().limit(1)
        )
        if run.kind == "main" and active is not None:
            raise RunAdmissionConflict(f"任务已有活跃 Main Run: {active.run_id}")
        self._normalize_mutable_payloads(run)
        session.add(run)
        dispatch = RunDispatch(
            dispatch_id=uuid.uuid5(uuid.NAMESPACE_URL, f"focus:run-dispatch:{run.run_id}").hex,
            run_id=run.run_id,
            status="accepted",
        )
        session.add(dispatch)
        await session.flush()
        return RunAdmissionResult(run, dispatch, True)

    @staticmethod
    def _normalize_mutable_payloads(run: DesktopRun) -> None:
        normalized = {
            "input_messages": PersistencePayloadNormalizer.normalize(run.input_messages or [], "desktop-run.input-messages"),
            "equipment": PersistencePayloadNormalizer.normalize(run.equipment or {}, "desktop-run.equipment"),
            "workspace_anchor": PersistencePayloadNormalizer.normalize(run.workspace_anchor or {}, "desktop-run.workspace-anchor"),
            "workspace_result": PersistencePayloadNormalizer.normalize(run.workspace_result or {}, "desktop-run.workspace-result"),
        }
        run.input_messages = normalized["input_messages"].value
        run.workspace_anchor = normalized["workspace_anchor"].value
        run.workspace_result = normalized["workspace_result"].value
        equipment = dict(normalized["equipment"].value)
        affected = {name: result.metadata() for name, result in normalized.items() if result.replacement_count}
        if affected:
            equipment["_persistence_safety"] = {"run_id": run.run_id, "fields": affected}
        run.equipment = equipment

    @staticmethod
    async def _dispatch(session: AsyncSession, run_id: str) -> RunDispatch:
        dispatch = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id))
        if dispatch is None:
            raise RunAdmissionConflict(f"幂等 Run 缺少 durable dispatch: {run_id}")
        return dispatch

    @staticmethod
    def _lock_key(namespace: str, value: str) -> int:
        digest = hashlib.sha256(f"run-admission:{namespace}:{value}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big", signed=True)
