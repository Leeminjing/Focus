r"""本文件对外提供 WorkspaceSlotLifecycle 与 WorkspaceRunReconciler。

输入为 slot、租约、执行锚点、当前 fingerprint、审计策略与截止时间；输出为安全保留/清理决定或
Writer 部分副作用 observation。具体工作流为先回收过期 lease，再阻止清理活动租约和未采用变更，
符合条件时先封锁 slot、由受管 provider 删除物理 worktree，最后写墓碑并保留 deleted identity；
中断 Reader 可重试，Writer 必须先观察 diff。示例：`await lifecycle.cleanup(slot_id)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.workspace_coordination.models import (
    RunExecutionAnchor,
    WorkspaceLease,
    WorkspaceSlot,
    WorkspaceSlotTombstone,
)
from backend.app.desktop.workspace_coordination.git_worktree import GitWorktreeIsolationProvider
from backend.app.desktop.workspace_coordination.isolation import IsolationResult


class WorkspaceSlotLifecycle:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def cleanup(self, slot_id: str, reason: str, force: bool = False) -> bool:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.slot_id == slot_id).with_for_update())
            if slot is None or slot.kind == "authoritative":
                return False
            await session.execute(
                update(WorkspaceLease)
                .where(WorkspaceLease.slot_id == slot_id, WorkspaceLease.status == "active", WorkspaceLease.expires_at <= now)
                .values(status="expired")
            )
            active = await session.scalar(select(WorkspaceLease.lease_id).where(WorkspaceLease.slot_id == slot_id, WorkspaceLease.status == "active").limit(1))
            unadopted = await session.scalar(select(RunExecutionAnchor.run_id).where(RunExecutionAnchor.slot_id == slot_id, RunExecutionAnchor.adoption_state.in_(["required", "pending", "conflict"])).limit(1))
            retained = slot.retention_until is not None and slot.retention_until > now
            if not force and (active or unadopted or retained):
                slot.lifecycle = "retained"
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
        try:
            await self._remove_physical(snapshot)
        except Exception as exc:
            async with self._sessions.begin() as session:
                slot = await session.get(WorkspaceSlot, slot_id, with_for_update=True)
                if slot is not None:
                    slot.lifecycle = "retained"
                    slot.metadata_json = {**(slot.metadata_json or {}), "cleanup_error": str(exc)[:1000]}
            return False
        async with self._sessions.begin() as session:
            slot = await session.get(WorkspaceSlot, slot_id, with_for_update=True)
            if slot is None or slot.lifecycle != "released":
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
        managed_root = root.parents[1]
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
