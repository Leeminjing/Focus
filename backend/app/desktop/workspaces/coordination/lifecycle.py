r"""本文件对外提供 WorkspaceSlotLifecycle 与 WorkspaceRunReconciler。

输入为 slot、租约、执行锚点、当前 fingerprint、审计策略与截止时间；输出为安全保留/清理决定或
Writer 部分副作用 observation。具体工作流为先回收过期 lease，再阻止清理活动租约和未采用变更，
符合条件时先封锁 slot、由受管 provider 删除物理 worktree，最后写墓碑并保留 deleted identity；
中断 Reader 可重试，Writer 必须先观察 diff；未确认 Shell 清理的 Run 阻止物理 slot 删除，包括强制清理。
默认清理还保留未结算或证据不全的 Run，并在删除前复核 fingerprint；删除与墓碑登记持有 slot 锁，取消等待物理操作收口。
示例：`await lifecycle.cleanup(slot_id, "retention_expired")`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from focus.config.layered import global_home

from backend.app.desktop.workspaces.coordination.models import (
    RunExecutionAnchor,
    WorkspaceLease,
    WorkspaceSlot,
    WorkspaceSlotTombstone,
)
from backend.app.desktop.workspaces.coordination.git_worktree import GitWorktreeIsolationProvider
from backend.app.desktop.workspaces.coordination.isolation import IsolationResult
from backend.app.desktop.persistence.models import DesktopRun
from backend.app.desktop.workspaces.coordination.reservations import reserved_adoption
from backend.app.desktop.workspaces.coordination.fingerprints import WorkspaceFingerprinter


class WorkspaceSlotLifecycle:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def cleanup(self, slot_id: str, reason: str, force: bool = False) -> bool:
        # Git runs in a thread. Shutdown cancellation must not release the slot
        # lock while that thread is still removing the directory.
        task = asyncio.create_task(self._cleanup(slot_id, reason, force))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise

    async def _cleanup(self, slot_id: str, reason: str, force: bool) -> bool:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.slot_id == slot_id).with_for_update())
            if slot is None or slot.kind == "authoritative" or slot.lifecycle == "deleted":
                return False
            if await reserved_adoption(session, slot_id):
                return False
            unresolved = await session.scalar(select(DesktopRun.run_id).where(
                DesktopRun.workspace_anchor["slot_id"].astext == slot_id, DesktopRun.settled_at.is_(None),
                DesktopRun.workspace_result["shell_cleanup"].astext.not_in(("{}", "null")),
            ).limit(1))
            if unresolved is not None:
                return False
            await session.execute(
                update(WorkspaceLease)
                .where(WorkspaceLease.slot_id == slot_id, WorkspaceLease.status == "active", WorkspaceLease.expires_at <= now)
                .values(status="expired")
            )
            active = await session.scalar(select(WorkspaceLease.lease_id).where(WorkspaceLease.slot_id == slot_id, WorkspaceLease.status == "active").limit(1))
            unadopted = await session.scalar(select(RunExecutionAnchor.run_id).where(RunExecutionAnchor.slot_id == slot_id, RunExecutionAnchor.adoption_state.in_(["required", "pending", "conflict"])).limit(1))
            retained = slot.retention_until is not None and slot.retention_until > now
            # An expired lease is not proof that a Run has finished using its files.
            unsettled = await session.scalar(select(DesktopRun.run_id).where(
                DesktopRun.settled_at.is_(None),
                (DesktopRun.workspace_anchor["slot_id"].astext == slot_id)
                | DesktopRun.run_id.in_(select(WorkspaceLease.run_id).where(WorkspaceLease.slot_id == slot_id))
                | DesktopRun.run_id.in_(select(RunExecutionAnchor.run_id).where(RunExecutionAnchor.slot_id == slot_id)),
            ).limit(1))
            uncertain = await session.scalar(select(RunExecutionAnchor.run_id).where(
                RunExecutionAnchor.slot_id == slot_id,
                (RunExecutionAnchor.settled_at.is_(None))
                | (RunExecutionAnchor.resulting_fingerprint.is_(None))
                | ((RunExecutionAnchor.resulting_fingerprint != RunExecutionAnchor.observed_fingerprint)
                   & (RunExecutionAnchor.adoption_state != "adopted")),
            ).limit(1))
            if not force and (active or unadopted or retained or unsettled or uncertain):
                return False
            slot.lifecycle = "released"
            snapshot = {
                "slot_id": slot.slot_id,
                "workspace_id": slot.workspace_id,
                "root_path": slot.root_path,
                "provider": slot.provider,
                "base_revision": slot.base_revision,
                "fingerprint": slot.current_fingerprint,
                "metadata": dict(slot.metadata_json or {}),
            }
            # Keep the slot lock through removal: competing cleanup, lease and adoption
            # operations must not race the filesystem effect. A crash rolls back the
            # claim; retrying a missing directory safely completes the tombstone.
            try:
                root = Path(slot.root_path)
                if not force and root.exists():
                    actual = await asyncio.to_thread(WorkspaceFingerprinter().capture, root)
                    if actual.digest != slot.current_fingerprint:
                        raise RuntimeError("隔离 workspace 文件已变化，保留目录等待核验")
                await self._remove_physical(snapshot)
            except Exception as exc:
                slot.lifecycle = "retained"
                slot.metadata_json = {**(slot.metadata_json or {}), "cleanup_error": str(exc)[:1000]}
                return False
            tombstone = await session.scalar(
                select(WorkspaceSlotTombstone).where(WorkspaceSlotTombstone.slot_id == slot_id)
            )
            if tombstone is None:
                session.add(
                    WorkspaceSlotTombstone(
                        tombstone_id=uuid.uuid4().hex,
                        slot_id=slot.slot_id,
                        workspace_id=slot.workspace_id,
                        root_path=slot.root_path,
                        final_fingerprint=slot.current_fingerprint,
                        reason=reason,
                        metadata_json=slot.metadata_json,
                    )
                )
            slot.lifecycle = "deleted"
            return True

    @staticmethod
    async def _remove_physical(snapshot: dict) -> None:
        if snapshot["provider"] != "git_worktree":
            raise RuntimeError("未知隔离 provider，已保留 slot 供人工清理")
        root = Path(snapshot["root_path"])
        managed_root = global_home() / "loop-workspaces" / snapshot["workspace_id"]
        cleanup_token = snapshot["metadata"].get("cleanup_token")
        if not cleanup_token:
            raise RuntimeError("隔离 slot 缺少 provider 清理句柄")
        provider = GitWorktreeIsolationProvider(managed_root)
        await provider.remove(
            IsolationResult(
                root_path=root,
                provider="git_worktree",
                baseline=snapshot["base_revision"],
                cleanup_token=str(cleanup_token),
            )
        )


class WorkspaceRunReconciler:
    @staticmethod
    def classify(anchor: RunExecutionAnchor, lease_mode: str, current_fingerprint: str) -> dict[str, object]:
        changed = current_fingerprint != anchor.observed_fingerprint
        if lease_mode == "read" and not changed:
            return {"retryable": True, "observation": None}
        return {
            "retryable": False,
            "observation": {
                "kind": "interrupted_workspace_effect",
                "run_id": anchor.run_id,
                "observed_fingerprint": anchor.observed_fingerprint,
                "current_fingerprint": current_fingerprint,
                "changed": changed,
                "effect_evidence": anchor.effect_evidence,
            },
        }
