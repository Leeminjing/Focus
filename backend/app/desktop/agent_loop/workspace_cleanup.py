"""Reclaim settled isolated worktrees after a durable 24-hour retention window.

The admission supervisor runs this independently of active Loop supervisors, so
completed/stopped Loops and expired retention windows are still revisited.
Unadopted or uncertain effects remain protected by WorkspaceSlotLifecycle.
"""

from datetime import UTC, datetime, timedelta
import logging

from sqlalchemy import select

from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.agent_loop.curation_ownership import TERMINAL_LOOP_STATUSES
from backend.app.desktop.workspace_coordination import (
    RunExecutionAnchor, WorkspaceAdoption, WorkspaceSlot, WorkspaceSlotLifecycle,
)


logger = logging.getLogger(__name__)


class LoopWorkspaceCleanup:
    def __init__(self, sessions, *, retention: timedelta = timedelta(hours=24)):
        if retention < timedelta(0):
            raise ValueError("Workspace retention must be nonnegative")
        self._sessions = sessions
        self._retention = retention
        self._lifecycle = WorkspaceSlotLifecycle(sessions)

    async def drain(self) -> int:
        removed = 0
        after = ""
        while True:
            async with self._sessions() as session:
                ids = tuple(await session.scalars(select(WorkspaceSlot.slot_id).where(
                    WorkspaceSlot.kind == "isolated", WorkspaceSlot.provider == "git_worktree",
                    WorkspaceSlot.lifecycle != "deleted", WorkspaceSlot.slot_id > after,
                ).order_by(WorkspaceSlot.slot_id).limit(100)))
            if not ids:
                return removed
            for slot_id in ids:
                try:
                    if await self._due(slot_id):
                        removed += int(await self._lifecycle.cleanup(slot_id, "retention_expired", force=False))
                except Exception:
                    # One broken provider/record must not starve other candidates.
                    logger.exception("Isolated workspace cleanup failed: %s", slot_id)
            after = ids[-1]

    async def _due(self, slot_id: str) -> bool:
        async with self._sessions.begin() as session:
            slot = await session.get(WorkspaceSlot, slot_id, with_for_update=True)
            if slot is None or slot.lifecycle == "deleted":
                return False
            anchors = tuple(await session.scalars(select(RunExecutionAnchor).where(
                RunExecutionAnchor.slot_id == slot_id)))
            if any(anchor.settled_at is None or anchor.resulting_fingerprint is None
                   or anchor.adoption_state in {"required", "pending", "conflict"}
                   or (anchor.resulting_fingerprint != anchor.observed_fingerprint
                       and anchor.adoption_state != "adopted") for anchor in anchors):
                return False
            completed = [anchor.settled_at for anchor in anchors]
            adopted = tuple(await session.scalars(select(WorkspaceAdoption.completed_at).where(
                WorkspaceAdoption.source_slot_id == slot_id, WorkspaceAdoption.status == "adopted")))
            completed.extend(stamp for stamp in adopted if stamp is not None)
            loop = await session.get(AgentLoop, slot.owner_loop_id) if slot.owner_loop_id else None
            if loop is not None and loop.status in TERMINAL_LOOP_STATUSES and loop.completed_at is not None:
                completed.append(loop.completed_at)
            elif not anchors:
                # A freshly prepared slot may not have its Run/lease yet.
                return False
            if not completed:
                return False
            deadline = max(completed) + self._retention
            if slot.retention_until is None or slot.retention_until < deadline:
                slot.retention_until = deadline
            return slot.retention_until <= datetime.now(UTC)
