r"""本文件对外提供 WorkspaceRunPlanner 与 WorkspaceRunPlan，作为 Loop 到 workspace 的排程端口。

输入为 Loop、待派发 directive、服务端持久化权限和当前 Workspace Slot；输出为每条可安全并发执行
directive 绑定的确定 slot。具体工作流为 Reader 共享空闲权威 slot，单个 Writer 可独占权威 slot；
并行波次及活动隔离 Writer 的补位均从同一干净 Git baseline 创建 Lane 专属 worktree。
隔离规划前后核对当前 Round 的工作区版本与活动 Writer 的 baseline；漂移时保留排队任务，
等待既有观察/授权流程重新确认，不能把权威目录的新 HEAD 当成旧波次输入。
活动 lease 与已准入 pending Run 都占用 slot，普通资源等待在 queued_reason 说明；未执行且基线、
fingerprint 完全匹配的 worktree 才能复用，已执行或 retained 成果保留，规划不初始化或提交源仓库。
配额耗尽时仅在没有活动 Writer/Reader 或已规划任务时允许一个权威 Writer 串行推进，其余排队，
避免待采用成果占满配额后整轮永久等待；一旦回退到权威 Writer，本次其余 Writer 全部排队。
示例：`plans = await planner.plan_wave(loop_id, directive_ids, concurrency=4)`。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from datetime import UTC, datetime
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopContextMembership,
    LoopDelegationGrant,
    LoopDirective,
    LoopRound,
)
from backend.app.desktop.workspace_coordination.fingerprints import WorkspaceFingerprinter
from backend.app.desktop.workspace_coordination.git_worktree import GitWorktreeIsolationProvider, WorkspaceIsolationCapacity
from backend.app.desktop.workspace_coordination.isolation import IsolationRequest
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceLease, WorkspaceSlot
from backend.app.desktop.workspace_coordination.schemas import WorkspaceFingerprint, WorkspaceIntentDeriver
from backend.app.desktop.models import DesktopRun, DesktopThread
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
                        .order_by(LoopDirective.created_at, LoopDirective.directive_id)
                    )
                ).all()
            )
            if grant is None or authoritative is None:
                return ()
            from backend.app.desktop.agent_loop.directive_equipment import resolve_directive_equipment

            readers = []
            writers = []
            for directive in directives[:concurrency]:
                intent = WorkspaceIntentDeriver.derive(await resolve_directive_equipment(session, loop, directive))
                (readers if intent.mode.value == "read" else writers).append(directive)
            occupied, writing = await self._activity(session, loop.workspace_id)
            other_claim = await session.scalar(select(LoopDirective.directive_id).where(
                LoopDirective.loop_id == loop_id, LoopDirective.status == "launching",
                LoopDirective.directive_id.not_in(directive_ids)).limit(1))
            allow_isolation = "isolate_workspace" in set(grant.capabilities or [])
            workspace_id = loop.workspace_id
            source_root = Path(authoritative.root_path)
        if other_claim:
            await self._queue(loop_id, directives, "workspace_dispatch_in_progress")
            return ()
        if authoritative.slot_id in writing:
            await self._queue(loop_id, directives, "authoritative_writer_active")
            return ()
        plans = [WorkspaceRunPlan(row.directive_id, authoritative.slot_id, "read") for row in readers]
        if not writers:
            return tuple(plans)
        authoritative_busy = authoritative.slot_id in occupied
        parallel = bool(writing) or authoritative_busy or bool(readers) or len(writers) > 1
        use_isolation = allow_isolation and parallel
        baseline = (
            await asyncio.to_thread(WorkspaceFingerprinter().capture, source_root)
            if use_isolation
            else None
        )
        if baseline is None or baseline.vcs_revision is None or baseline.dirty:
            reason = ("workspace_isolation_not_authorized" if not allow_isolation else
                      "workspace_not_git" if baseline and not baseline.vcs_revision else "workspace_baseline_dirty")
            if not authoritative_busy and not readers and not writing:
                first = writers.pop(0)
                plans.append(WorkspaceRunPlan(first.directive_id, authoritative.slot_id, "write"))
            await self._queue(loop_id, writers, reason)
            return tuple(plans)
        round_id = directives[0].round_id
        if not await self._baseline_is_current(loop_id, round_id, authoritative, baseline):
            await self._queue(loop_id, directives, "workspace_baseline_changed")
            return ()
        for index, directive in enumerate(writers):
            lane_id = await self._lane_id(loop_id, directive.target_context_id)
            if lane_id is None:
                continue
            try:
                slot = await self._isolated_slot(workspace_id, loop_id, lane_id, source_root, baseline.vcs_revision)
            except WorkspaceIsolationCapacity:
                if not plans and not occupied and not writing:
                    plans.append(WorkspaceRunPlan(directive.directive_id, authoritative.slot_id, "write"))
                    await self._queue(loop_id, writers[index + 1:], "workspace_isolation_quota")
                    break
                else:
                    await self._queue(loop_id, [directive], "workspace_isolation_quota")
                continue
            if slot is not None:
                plans.append(WorkspaceRunPlan(directive.directive_id, slot.slot_id, "write"))
        if not await self._baseline_is_current(loop_id, round_id, authoritative, baseline):
            await self._queue(loop_id, directives, "workspace_baseline_changed")
            return ()
        return tuple(plans)

    async def _baseline_is_current(
        self, loop_id: str, round_id: str, slot: WorkspaceSlot, baseline: WorkspaceFingerprint,
    ) -> bool:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, loop_id)
            round_row = await session.get(LoopRound, round_id)
            current = await session.get(WorkspaceSlot, slot.slot_id)
            if (loop is None or loop.status != "running" or loop.current_round_id != round_id
                    or round_row is None or round_row.status not in {"ready", "running"}
                    or round_row.authority_revision != loop.authority_revision or round_row.goal_revision != loop.goal_revision
                    or current is None or current.lifecycle != "active" or current.revision != slot.revision
                    or current.revision != round_row.workspace_revision or current.current_fingerprint != baseline.digest):
                return False
            _, writing = await self._activity(session, slot.workspace_id)
            commits = set(await session.scalars(select(WorkspaceSlot.base_revision).where(
                WorkspaceSlot.slot_id.in_(writing), WorkspaceSlot.kind == "isolated")))
            if slot.slot_id in writing or (commits and commits != {baseline.vcs_revision}):
                return False
        head, dirty = await asyncio.to_thread(WorkspaceFingerprinter.git_state, Path(slot.root_path))
        return head == baseline.vcs_revision and not dirty

    @staticmethod
    async def _activity(session: AsyncSession, workspace_id: str) -> tuple[set[str], set[str]]:
        leases = (await session.execute(select(WorkspaceLease.slot_id, WorkspaceLease.mode).join(
            WorkspaceSlot, WorkspaceSlot.slot_id == WorkspaceLease.slot_id).where(
            WorkspaceSlot.workspace_id == workspace_id, WorkspaceLease.status == "active",
            WorkspaceLease.expires_at > datetime.now(UTC)))).all()
        occupied = {slot_id for slot_id, _ in leases}
        writing = {slot_id for slot_id, mode in leases if mode == "write"}
        runs = (await session.scalars(select(DesktopRun).join(DesktopThread,
            DesktopThread.task_id == DesktopRun.task_id).where(DesktopThread.workspace_id == workspace_id,
            DesktopRun.status.in_(("pending", "running"))))).all()
        for run in runs:
            slot_id = (run.workspace_anchor or {}).get("slot_id")
            if slot_id:
                occupied.add(slot_id)
                if WorkspaceIntentDeriver.derive(run.equipment or {}).mode.value == "write":
                    writing.add(slot_id)
        return occupied, writing

    async def _queue(self, loop_id: str, directives: list[LoopDirective], reason: str) -> None:
        if not directives:
            return
        async with self._sessions.begin() as session:
            await session.get(AgentLoop, loop_id, with_for_update=True)
            await session.execute(update(LoopDirective).where(LoopDirective.loop_id == loop_id,
                LoopDirective.directive_id.in_([row.directive_id for row in directives]),
                LoopDirective.status == "launching").values(queued_reason=reason))

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
                    WorkspaceSlot.lifecycle == "active",
                    WorkspaceSlot.base_revision == baseline,
                ).order_by(WorkspaceSlot.created_at.desc()).limit(1)
            )
            if existing is not None:
                used = await session.scalar(select(RunExecutionAnchor.run_id).where(
                    RunExecutionAnchor.slot_id == existing.slot_id).limit(1))
                occupied, _ = await self._activity(session, workspace_id)
                if not used and existing.slot_id not in occupied and Path(existing.root_path).is_dir():
                    fingerprint = await asyncio.to_thread(WorkspaceFingerprinter().capture, Path(existing.root_path))
                    if (fingerprint.vcs_revision == baseline and not fingerprint.dirty
                            and fingerprint.digest == existing.current_fingerprint):
                        return existing
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
        try:
            fingerprint = await asyncio.to_thread(WorkspaceFingerprinter().capture, result.root_path)
        except BaseException:
            await provider.remove(result)
            raise
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
        except BaseException:
            async with self._sessions() as session:
                persisted = await session.get(WorkspaceSlot, slot_id)
            if persisted is None:
                await provider.remove(result)
            raise
