r"""本文件对外提供 RunLifecycleFinalizer 与 RunSettlement。

输入为已结束 RunRecord或未启动成功的持久 Run、最终 checkpoint 和 workspace result；输出为终态 Run、
新 Context revision 与 MainRunSettled event identity。具体工作流为先在事务外精确读取执行 checkpoint，
再在单事务中锁 Run、保存终态及完整模型用量、按 base revision CAS 发布 checkpoint revision、按 lease 模式结算
workspace effect、释放 lease 并 enqueue outbox；Reader 只记录并发变化，隔离 Writer 保留待采用结果，
权威 Writer 才推进权威 slot。重复 finalize 返回同一事实，陈旧 Context 不覆盖用户的新 current pointer。
示例：`settlement = await finalizer.finalize(record)`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
from pathlib import Path
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
    ContextRevisionSourceContract,
)
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.workspace_coordination.fingerprints import WorkspaceFingerprinter
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceLease, WorkspaceSlot
from backend.app.desktop.run_orchestration.outbox import RunOutboxRepository
from focus.runtime.runs.manager import RunRecord


class RunSettlement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    task_id: str
    kind: str
    status: str
    final_checkpoint_id: str | None
    context_revision: ContextRevisionRef | None
    context_publication: str
    event_id: str
    idempotent: bool = False


class RunLifecycleFinalizer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        context_revisions: ContextRevisionRepository | None = None,
        outbox: RunOutboxRepository | None = None,
    ) -> None:
        self._sessions = session_factory
        self._checkpointer = checkpointer
        self._contexts = context_revisions or ContextRevisionRepository()
        self._outbox = outbox or RunOutboxRepository()

    async def finalize(
        self,
        record: RunRecord,
        workspace_result: dict[str, Any] | None = None,
    ) -> RunSettlement:
        run, thread = await self._identity(record.run_id)
        checkpoint_id = await self._latest_checkpoint(run, thread)
        captured = await self._capture_workspace(run)
        async with self._sessions.begin() as session:
            locked = await session.scalar(
                select(DesktopRun)
                .where(DesktopRun.run_id == record.run_id)
                .with_for_update()
            )
            if locked is None:
                raise LookupError(f"Run 不存在: {record.run_id}")
            if locked.settled_at is not None:
                return await self._settlement(session, locked, idempotent=True)
            terminal_status = record.status.value
            terminal_error = record.error
            if terminal_status in {"pending", "running"}:
                terminal_status = "error"
                terminal_error = terminal_error or "Run task 已结束但未产生终态"
            locked.status = terminal_status
            locked.error = terminal_error
            locked.model_call_count = record.model_call_count
            locked.prompt_input_tokens = record.prompt_input_tokens
            locked.prompt_output_tokens = record.prompt_output_tokens
            locked.prompt_cache_hit_tokens = record.prompt_cache_hit_tokens
            locked.final_checkpoint_id = checkpoint_id
            locked.workspace_result = await self._settle_workspace(session, locked, workspace_result, captured)
            locked.settled_at = datetime.now(UTC)
            await self._release_workspace_lease(session, locked)
            revision, publication = await self._publish_context_checkpoint(
                session, locked, thread, checkpoint_id
            )
            event = await self._outbox.enqueue_settled(
                session,
                locked.run_id,
                self._payload(locked, revision, publication),
            )
            return RunSettlement(
                run_id=locked.run_id,
                task_id=locked.task_id,
                kind=locked.kind,
                status=locked.status,
                final_checkpoint_id=checkpoint_id,
                context_revision=revision,
                context_publication=publication,
                event_id=event.event_id,
            )

    async def reconcile_active(self, reason: str) -> int:
        async with self._sessions() as session:
            identities = list(
                (
                    await session.scalars(
                        select(DesktopRun).where(DesktopRun.status.in_(["pending", "running"]))
                    )
                ).all()
            )
        captured_by_run = {
            run.run_id: await self._capture_workspace(run)
            for run in identities
        }
        async with self._sessions.begin() as session:
            runs = list(
                (
                    await session.scalars(
                        select(DesktopRun)
                        .where(DesktopRun.status.in_(["pending", "running"]))
                        .order_by(DesktopRun.run_id)
                        .with_for_update()
                    )
                ).all()
            )
            for run in runs:
                run.status = "interrupted"
                run.error = reason
                run.settled_at = datetime.now(UTC)
                reconciliation = await self._reconcile_workspace_evidence(
                    session,
                    run,
                    captured_by_run.get(run.run_id),
                )
                run.workspace_result = {
                    **(run.workspace_result or {}),
                    "context_publication": "unknown_after_restart",
                    "reconciliation": reconciliation,
                }
                await self._release_workspace_lease(session, run)
                await self._outbox.enqueue_settled(
                    session,
                    run.run_id,
                    self._payload(run, None, "unknown_after_restart"),
                )
            return len(runs)

    @staticmethod
    async def _reconcile_workspace_evidence(session: AsyncSession, run: DesktopRun, captured) -> dict[str, Any]:
        anchor = await session.get(RunExecutionAnchor, run.run_id, with_for_update=True)
        mode = str((run.workspace_anchor or {}).get("mode") or "read")
        if anchor is None or captured is None:
            return {
                "mode": mode,
                "evidence": "unavailable",
                "retry_safe": False,
                "side_effect_observed": None,
            }
        changed = captured.digest != anchor.observed_fingerprint
        evidence = {
            "kind": "restart_workspace_reconciliation",
            "mode": mode,
            "before": anchor.observed_fingerprint,
            "after": captured.digest,
            "changed": changed,
        }
        anchor.effect_evidence = [*(anchor.effect_evidence or []), evidence]
        anchor.resulting_workspace_revision = anchor.observed_workspace_revision
        anchor.resulting_fingerprint = captured.digest
        anchor.settled_at = datetime.now(UTC)
        return {
            "mode": mode,
            "evidence": "fingerprint",
            "retry_safe": mode == "read" and not changed,
            "side_effect_observed": changed,
            "before": anchor.observed_fingerprint,
            "after": captured.digest,
        }

    async def abort_prepared(self, run_id: str, reason: str) -> bool:
        async with self._sessions.begin() as session:
            run = await session.get(DesktopRun, run_id, with_for_update=True)
            if run is None or run.settled_at is not None:
                return False
            run.status = "error"
            run.error = reason
            run.settled_at = datetime.now(UTC)
            run.workspace_result = {
                **(run.workspace_result or {}),
                "context_publication": "not_started",
                "launch_error": reason,
            }
            await self._release_workspace_lease(session, run)
            await self._outbox.enqueue_settled(
                session,
                run.run_id,
                self._payload(run, None, "not_started"),
            )
            return True

    async def _identity(self, run_id: str) -> tuple[DesktopRun, DesktopThread]:
        async with self._sessions() as session:
            run = await session.get(DesktopRun, run_id)
            if run is None:
                raise LookupError(f"Run 不存在: {run_id}")
            thread = await session.get(DesktopThread, run.task_id)
            if thread is None:
                raise LookupError(f"Run Context 不存在: {run.task_id}")
            return run, thread

    async def _latest_checkpoint(
        self,
        run: DesktopRun,
        thread: DesktopThread,
    ) -> str | None:
        execution_thread = run.execution_thread_id or thread.thread_id
        checkpoint = await self._checkpointer.aget_tuple(
            {
                "configurable": {
                    "thread_id": execution_thread,
                    "checkpoint_ns": run.checkpoint_ns or "",
                }
            }
        )
        if checkpoint is None:
            return None
        return checkpoint.config.get("configurable", {}).get("checkpoint_id")

    async def _publish_context_checkpoint(
        self,
        session: AsyncSession,
        run: DesktopRun,
        thread: DesktopThread,
        checkpoint_id: str | None,
    ) -> tuple[ContextRevisionRef | None, str]:
        if run.kind != "main" or checkpoint_id is None:
            return None, "not_applicable"
        current = await self._contexts.current(session, run.task_id)
        if current is not None and current.ref.checkpoint_id == checkpoint_id:
            return current.ref, "unchanged"
        if (
            run.context_revision_id is not None
            and (current is None or current.ref.revision_id != run.context_revision_id)
        ):
            return None, "superseded"
        expected = current.ref if current is not None else None
        revision_id = uuid.uuid4().hex
        ref = ContextRevisionRef(
            context_id=run.task_id,
            revision_id=revision_id,
            generation=await self._contexts.next_generation(session, run.task_id),
            execution_thread_id=run.execution_thread_id or thread.thread_id,
            checkpoint_ns=run.checkpoint_ns or "",
            checkpoint_id=checkpoint_id,
            payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
        )
        sources = (
            (ContextRevisionSourceContract(source=expected, position=0),)
            if expected is not None
            else ()
        )
        contract = ContextRevisionContract(
            ref=ref,
            sources=sources,
            content_hash=hashlib.sha256(
                f"run-settled:{run.run_id}:{checkpoint_id}".encode()
            ).hexdigest(),
            projection_status=ContextRevisionProjectionStatus.VALID,
            origin_kind=ContextRevisionOriginKind.RUN_SETTLED,
            origin_id=run.run_id,
            created_at=datetime.now(UTC),
        )
        await self._contexts.insert(session, contract)
        await self._contexts.switch_current(session, ref, expected)
        return ref, "published"

    async def _settlement(
        self,
        session: AsyncSession,
        run: DesktopRun,
        *,
        idempotent: bool,
    ) -> RunSettlement:
        event_id = self._outbox.event_id(run.run_id, "MainRunSettled")
        revision = None
        publication = str((run.workspace_result or {}).get("context_publication") or "unknown")
        if run.kind == "main" and run.final_checkpoint_id is not None:
            current = await self._contexts.current(session, run.task_id)
            if current is not None and current.ref.checkpoint_id == run.final_checkpoint_id:
                revision = current.ref
                publication = "published"
        return RunSettlement(
            run_id=run.run_id,
            task_id=run.task_id,
            kind=run.kind,
            status=run.status,
            final_checkpoint_id=run.final_checkpoint_id,
            context_revision=revision,
            context_publication=publication,
            event_id=event_id,
            idempotent=idempotent,
        )

    async def _capture_workspace(self, run: DesktopRun):
        slot_id = (run.workspace_anchor or {}).get("slot_id")
        if not slot_id:
            return None
        async with self._sessions() as session:
            slot = await session.get(WorkspaceSlot, slot_id)
        if slot is None or not Path(slot.root_path).is_dir():
            return None
        return await asyncio.to_thread(WorkspaceFingerprinter().capture, Path(slot.root_path))

    @staticmethod
    async def _settle_workspace(
        session: AsyncSession,
        run: DesktopRun,
        supplied: dict[str, Any] | None,
        captured,
    ) -> dict[str, Any]:
        result = dict(supplied or {})
        anchor_data = run.workspace_anchor or {}
        slot_id = anchor_data.get("slot_id")
        if not slot_id:
            return result
        slot = await session.get(WorkspaceSlot, slot_id, with_for_update=True)
        anchor = await session.get(RunExecutionAnchor, run.run_id, with_for_update=True)
        if slot is None or anchor is None:
            return {**result, "workspace_status": "anchor_missing"}
        expected = anchor.observed_workspace_revision
        if slot.revision != expected:
            return {
                **result,
                "workspace_status": "superseded",
                "revision": slot.revision,
                "fingerprint": slot.current_fingerprint,
            }
        mode = str(anchor_data.get("mode") or "read")
        captured_fingerprint = captured.digest if captured is not None else slot.current_fingerprint
        changed = captured_fingerprint != anchor.observed_fingerprint
        effect = {
            "kind": "workspace_fingerprint_transition",
            "mode": mode,
            "slot_kind": slot.kind,
            "before": anchor.observed_fingerprint,
            "after": captured_fingerprint,
            "changed": changed,
        }
        anchor.effect_evidence = [*(anchor.effect_evidence or []), effect]
        if mode == "read" and changed:
            slot.revision += 1
            slot.current_fingerprint = captured_fingerprint
            anchor.resulting_workspace_revision = slot.revision
            anchor.resulting_fingerprint = captured_fingerprint
            anchor.settled_at = datetime.now(UTC)
            return {
                **result,
                "workspace_status": "read_lease_violation",
                "revision": slot.revision,
                "fingerprint": slot.current_fingerprint,
                "effect_evidence": anchor.effect_evidence,
            }
        if mode == "write" and changed:
            slot.revision += 1
            slot.current_fingerprint = captured_fingerprint
        if slot.kind == "isolated" and mode == "write" and changed:
            anchor.adoption_state = "pending"
        anchor.resulting_workspace_revision = slot.revision
        anchor.resulting_fingerprint = slot.current_fingerprint
        anchor.settled_at = datetime.now(UTC)
        result.update(
            {
                "workspace_status": "adoption_pending" if anchor.adoption_state == "pending" else "settled",
                "revision": slot.revision,
                "fingerprint": slot.current_fingerprint,
                "effect_evidence": anchor.effect_evidence,
                "adoption_state": anchor.adoption_state,
            }
        )
        return result

    @staticmethod
    async def _release_workspace_lease(session: AsyncSession, run: DesktopRun) -> None:
        lease_id = (run.workspace_anchor or {}).get("lease_id")
        if not lease_id:
            return
        lease = await session.get(WorkspaceLease, lease_id, with_for_update=True)
        if lease is not None and lease.status == "active":
            lease.status = "released"
            lease.released_at = datetime.now(UTC)

    @staticmethod
    def _payload(
        run: DesktopRun,
        revision: ContextRevisionRef | None,
        publication: str,
    ) -> dict[str, Any]:
        result = dict(run.workspace_result or {})
        result["context_publication"] = publication
        run.workspace_result = result
        return {
            "run_id": run.run_id,
            "task_id": run.task_id,
            "kind": run.kind,
            "origin": run.origin,
            "status": run.status,
            "context_revision": revision.model_dump(mode="json") if revision else None,
            "final_checkpoint_id": run.final_checkpoint_id,
            "workspace_anchor": run.workspace_anchor,
            "workspace_result": result,
            "loop_id": run.loop_id,
            "round_id": run.round_id,
            "action_id": run.action_id,
            "directive_id": run.directive_id,
        }
