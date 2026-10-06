r"""本文件对外提供 RunAdmissionService 与 RunAdmissionResult。

输入为调用方事务中已构造但未提交的 DesktopRun；输出为同事务持久化的唯一 Run 与 accepted RunDispatch。
具体工作流为已存在的幂等 Loop Run 直接重放；新 Loop Run 先取得权威控制锁，再取得 task/idempotency 互斥锁及 Main Run 行锁，
锁后再次核对幂等赢家，仅 Main 执行查询活跃 Main，随后复核父链、绑定输入来源并同时写 Run/dispatch；控制与准入均按 Loop→Run 顺序，不创建 Agent。
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
from backend.app.desktop.run_orchestration.input_provenance import bind_run_inputs
from backend.app.desktop.run_orchestration.lineage import validate_parent_chain


@dataclass(frozen=True, slots=True)
class RunAdmissionResult:
    run: DesktopRun
    dispatch: RunDispatch
    created: bool


class RunAdmissionConflict(RuntimeError):
    pass


class RunAdmissionService:
    def __init__(self, ownership_validator=None):
        self._ownership_validator = ownership_validator

    async def admit(self, session: AsyncSession, run: DesktopRun) -> RunAdmissionResult:
        if run.loop_id is not None or run.round_id is not None:
            if run.idempotency_key:
                existing = await session.scalar(select(DesktopRun).where(DesktopRun.idempotency_key == run.idempotency_key))
                if existing is not None:
                    return RunAdmissionResult(existing, await self._dispatch(session, existing.run_id), False)
            if self._ownership_validator is None:
                raise ValueError("Loop Run 缺少注入的权威来源验证器")
            await self._ownership_validator(session, run)
        await session.execute(select(func.pg_advisory_xact_lock(self._lock_key("task", run.task_id))))
        if run.idempotency_key:
            await session.execute(select(func.pg_advisory_xact_lock(self._lock_key("idempotency", run.idempotency_key))))
            existing = await session.scalar(
                select(DesktopRun).where(DesktopRun.idempotency_key == run.idempotency_key).with_for_update()
            )
            if existing is not None:
                dispatch = await self._dispatch(session, existing.run_id)
                return RunAdmissionResult(existing, dispatch, False)
        if run.kind == "main":
            active = await session.scalar(
                select(DesktopRun).where(
                    DesktopRun.task_id == run.task_id,
                    DesktopRun.kind == "main",
                    DesktopRun.status.in_(("pending", "running")),
                    DesktopRun.run_id != run.run_id,
                ).with_for_update().limit(1)
            )
            if active is not None:
                raise RunAdmissionConflict(f"任务已有活跃 Main Run: {active.run_id}")
        await validate_parent_chain(session, run)
        run.input_messages = bind_run_inputs(run)
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
