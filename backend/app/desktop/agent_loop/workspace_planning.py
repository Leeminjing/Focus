r"""本文件对外提供 WorkspaceRunPlanner 与 WorkspaceRunPlan，作为 Loop 到 workspace 的排程端口。

输入为 Loop、待派发 directive、服务端持久化权限和当前 Workspace Slot；输出为每条可安全并发执行
directive 绑定的确定 slot。具体工作流为 Reader 共享权威 slot，首个 Writer 使用空闲权威 slot，额外
Writer 仅在授权且 Git 基线干净时创建或复用 Lane 专属 worktree；非 Git 或脏基线保持单 Writer 排队。
示例：`plans = await planner.plan_wave(loop_id, directive_ids, concurrency=4)`。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopContextMembership,
    LoopDelegationGrant,
    LoopDirective,
)
from backend.app.desktop.workspace_coordination.fingerprints import WorkspaceFingerprinter
from backend.app.desktop.workspace_coordination.git_worktree import GitWorktreeIsolationProvider
from backend.app.desktop.workspace_coordination.isolation import IsolationRequest
from backend.app.desktop.workspace_coordination.models import WorkspaceLease, WorkspaceSlot
from backend.app.desktop.workspace_coordination.schemas import WorkspaceIntentDeriver
from focus.config.layered import global_home


@dataclass(frozen=True, slots=True)
class WorkspaceRunPlan:
    directive_id: str
    slot_id: str
    mode: str


class WorkspaceRunPlanner:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def plan_wave(
        self,
        loop_id: str,
        directive_ids: tuple[str, ...],
        concurrency: int,
    ) -> tuple[WorkspaceRunPlan, ...]:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, loop_id)
            if loop is None:
                return ()
            grant = await session.scalar(
                select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == loop_id,
                    LoopDelegationGrant.revision == loop.authority_revision,
                    LoopDelegationGrant.status == "active",
                )
            )
            authoritative = await session.scalar(
                select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == loop.workspace_id,
                    WorkspaceSlot.kind == "authoritative",
                    WorkspaceSlot.lifecycle == "active",
                )
            )
            directives = list(
                (
                    await session.scalars(
                        select(LoopDirective)
                        .where(LoopDirective.directive_id.in_(directive_ids))
                        .order_by(LoopDirective.created_at)
                    )
                ).all()
            )
            if grant is None or authoritative is None:
                return ()
            intent = WorkspaceIntentDeriver.derive(loop.equipment or {})
            if intent.mode.value == "read":
                return tuple(
                    WorkspaceRunPlan(row.directive_id, authoritative.slot_id, "read")
                    for row in directives[:concurrency]
                )
            authoritative_busy = await session.scalar(
                select(WorkspaceLease.lease_id).where(
                    WorkspaceLease.slot_id == authoritative.slot_id,
                    WorkspaceLease.status == "active",
                ).limit(1)
            )
            allow_isolation = "isolate_workspace" in set(grant.capabilities or [])
            workspace_id = loop.workspace_id
            source_root = Path(authoritative.root_path)
        plans: list[WorkspaceRunPlan] = []
        remaining = directives[:concurrency]
        use_isolation = allow_isolation and (bool(authoritative_busy) or len(remaining) > 1)
        baseline = (
            await asyncio.to_thread(WorkspaceFingerprinter().capture, source_root)
            if use_isolation
            else None
        )
        if baseline is None or baseline.vcs_revision is None or baseline.dirty:
            if not authoritative_busy and remaining:
                first = remaining.pop(0)
                plans.append(WorkspaceRunPlan(first.directive_id, authoritative.slot_id, "write"))
            return tuple(plans)
        for directive in remaining:
            lane_id = await self._lane_id(loop_id, directive.target_context_id)
            if lane_id is None:
                continue
            slot = await self._isolated_slot(
                workspace_id,
                loop_id,
                lane_id,
                source_root,
                baseline.vcs_revision,
            )
            if slot is not None:
                plans.append(WorkspaceRunPlan(directive.directive_id, slot.slot_id, "write"))
        return tuple(plans)

    async def _lane_id(self, loop_id: str, context_id: str) -> str | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(LoopContextMembership.lane_id).where(
                    LoopContextMembership.loop_id == loop_id,
                    LoopContextMembership.context_id == context_id,
                    LoopContextMembership.status == "active",
                ).limit(1)
            )

    async def _isolated_slot(
        self,
        workspace_id: str,
        loop_id: str,
        lane_id: str,
        source_root: Path,
        baseline: str,
    ) -> WorkspaceSlot | None:
        async with self._sessions() as session:
            existing = await session.scalar(
                select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == workspace_id,
                    WorkspaceSlot.kind == "isolated",
                    WorkspaceSlot.owner_loop_id == loop_id,
                    WorkspaceSlot.owner_lane_id == lane_id,
                    WorkspaceSlot.lifecycle.in_(["active", "retained"]),
                ).order_by(WorkspaceSlot.created_at.desc()).limit(1)
            )
            if existing is not None:
                active = await session.scalar(
                    select(WorkspaceLease.lease_id).where(
                        WorkspaceLease.slot_id == existing.slot_id,
                        WorkspaceLease.mode == "write",
                        WorkspaceLease.status == "active",
                    ).limit(1)
                )
                return None if active else existing
        managed_root = global_home() / "loop-workspaces" / workspace_id
        provider = GitWorktreeIsolationProvider(managed_root)
        slot_id = uuid.uuid4().hex
        target = managed_root / loop_id / f"{lane_id}-{slot_id[:8]}"
        result = await provider.prepare(
            IsolationRequest(
                source_root=source_root,
                target_root=target,
                baseline=baseline,
                loop_id=loop_id,
                lane_id=lane_id,
            )
        )
        fingerprint = await asyncio.to_thread(WorkspaceFingerprinter().capture, result.root_path)
        slot = WorkspaceSlot(
            slot_id=slot_id,
            workspace_id=workspace_id,
            kind="isolated",
            root_path=str(result.root_path),
            provider=result.provider,
            base_revision=result.baseline,
            current_fingerprint=fingerprint.digest,
            revision=1,
            owner_loop_id=loop_id,
            owner_lane_id=lane_id,
            metadata_json={"cleanup_token": result.cleanup_token},
        )
        try:
            async with self._sessions.begin() as session:
                session.add(slot)
            return slot
        except Exception:
            await provider.remove(result)
            raise
