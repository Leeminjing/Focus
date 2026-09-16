r"""本文件对外提供 AgentLoopService 创建、查询、控制、用户覆盖与事件读取用例。

输入为认证后的 LoopCreateRequest、Loop id、控制动作或直接用户新目标；输出为完整 Loop 快照与
持久事件。具体工作流为 start 原子创建 goal/grant/holder/membership/budget/首轮，pause/resume/stop
推进 revision，stop 撤销未派发指令并中断活动 Run，override 使未提交 Patrol 工作 superseded 并建立
新观察轮。示例：`await service.start(body)`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopContextMembership, LoopDecision, LoopDelegationGrant, LoopDirective, LoopEventOutbox, LoopGoalRevision, LoopRound
from backend.app.desktop.agent_loop.schemas import LoopCreateRequest
from backend.app.desktop.context_curation.models import CurationLane, CurationProgram, CurationSourceSubscription, PortfolioLaneCandidate, PortfolioRevision
from backend.app.desktop.context_evolution.models import ContextRevision
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.workspace_coordination.fingerprints import WorkspaceFingerprinter
from backend.app.desktop.workspace_coordination.models import WorkspaceLease, WorkspaceSlot
from backend.app.desktop.agent_loop.budgets import configured_provider_count


class AgentLoopService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], run_manager=None) -> None:
        self._sessions = sessions
        self._run_manager = run_manager

    async def start(self, request: LoopCreateRequest) -> dict:
        if "request_completion" not in request.capabilities:
            raise HTTPException(422, "无人值守 Loop 必须明确是否允许 Patrol 请求完成")
        async with self._sessions.begin() as session:
            existing = await session.get(AgentLoop, request.loop_id)
            if existing is not None:
                return await self._snapshot(session, existing)
            context = await session.get(DesktopThread, request.initial_context_id)
            workspace = await session.get(DesktopWorkspace, request.workspace_id)
            if context is None or workspace is None or context.workspace_id != workspace.workspace_id:
                raise HTTPException(422, "初始 Context 与 workspace 不匹配")
            if context.current_revision_id is None:
                raise HTTPException(409, "初始 Context 尚无可执行 revision")
            context_revision = await session.get(ContextRevision, context.current_revision_id)
            if context_revision is None:
                raise HTTPException(409, "初始 Context revision 不存在")
            program_id = uuid.uuid4().hex
            lane_id = uuid.uuid4().hex
            portfolio_id = uuid.uuid4().hex
            frontier = [{"context_id": context.task_id, "revision_id": context_revision.revision_id, "checkpoint_id": context_revision.checkpoint_id}]
            frontier_hash = self._hash(frontier)
            program = CurationProgram(program_id=program_id, workspace_id=workspace.workspace_id, policy={"owner_loop_id": request.loop_id}, revision=1)
            session.add(program)
            await session.flush()
            loop = AgentLoop(loop_id=request.loop_id, workspace_id=request.workspace_id, initial_context_id=request.initial_context_id, program_id=program_id, holder_id=request.holder_id, status="running", health="observing", equipment=request.equipment)
            session.add(loop)
            await session.flush()
            goal = LoopGoalRevision(goal_revision_id=uuid.uuid4().hex, loop_id=loop.loop_id, revision=1, goal=request.goal, task_contract=request.task_contract, acceptance_criteria=list(request.acceptance_criteria), authored_by="user")
            grant = LoopDelegationGrant(grant_id=uuid.uuid4().hex, loop_id=loop.loop_id, revision=1, holder_id=request.holder_id, capabilities=list(request.capabilities), context_scope=list(request.context_scope), permission_scope=list(request.permission_scope), budgets=request.budgets.model_dump(), delegable_gates=list(request.delegable_gates), expires_at=datetime.fromisoformat(request.expires_at) if request.expires_at else None)
            subscription = CurationSourceSubscription(subscription_id=uuid.uuid4().hex, program_id=program_id, source_context_id=context.task_id, source_role="initial", selection_policy={}, position=0)
            lane = CurationLane(lane_id=lane_id, program_id=program_id, managed_context_id=context.task_id, purpose="Primary execution", normalized_purpose="primary execution", lane_policy={}, current_source_frontier_hash=frontier_hash, current_semantic_fingerprint=context_revision.content_hash)
            session.add_all([subscription, lane])
            await session.flush()
            portfolio = PortfolioRevision(portfolio_revision_id=portfolio_id, program_id=program_id, generation=1, source_frontier=frontier, frontier_hash=frontier_hash, base_program_revision=1, control_revisions={"loop_revision": 1, "grant_revision": 1, "workspace_revision": "1"}, target_lanes=[{"lane_id": lane_id, "action": "keep"}], status="published", completed_at=datetime.now(UTC), published_at=datetime.now(UTC))
            session.add(portfolio)
            await session.flush()
            candidate = PortfolioLaneCandidate(candidate_id=uuid.uuid4().hex, portfolio_revision_id=portfolio_id, lane_id=lane_id, action="keep", target_context_id=context.task_id, base_publisher_epoch=1, base_context_revision_id=context_revision.revision_id, candidate_context_revision_id=context_revision.revision_id, purpose=lane.purpose, source_allocation=frontier, source_frontier_hash=frontier_hash, semantic_fingerprint=context_revision.content_hash, status="unchanged")
            program.current_portfolio_revision_id = portfolio_id
            loop.current_portfolio_revision_id = portfolio_id
            membership = LoopContextMembership(membership_id=uuid.uuid4().hex, loop_id=loop.loop_id, context_id=request.initial_context_id, lane_id=lane_id, role="primary")
            slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == workspace.workspace_id, WorkspaceSlot.kind == "authoritative", WorkspaceSlot.lifecycle != "deleted").with_for_update())
            if slot is None:
                fingerprint = await asyncio.to_thread(
                    WorkspaceFingerprinter().capture,
                    Path(workspace.path),
                )
                slot = WorkspaceSlot(
                    slot_id=uuid.uuid4().hex,
                    workspace_id=workspace.workspace_id,
                    kind="authoritative",
                    root_path=workspace.path,
                    provider="git" if fingerprint.vcs_revision else "local",
                    base_revision=fingerprint.vcs_revision,
                    current_fingerprint=fingerprint.digest,
                    revision=1,
                )
                session.add(slot)
            round_row = LoopRound(round_id=uuid.uuid4().hex, loop_id=loop.loop_id, number=1, authority_revision=1, goal_revision=1, frontier_hash=frontier_hash, workspace_revision=slot.revision)
            loop.current_round_id = round_row.round_id
            session.add_all([goal, grant, candidate, membership, round_row, LoopBudgetUsage(loop_id=loop.loop_id), LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop.loop_id, sequence=1, event_type="LoopStarted", payload={"round_id": round_row.round_id, "program_id": program_id, "portfolio_revision_id": portfolio_id}, idempotency_key=f"loop:{loop.loop_id}:started")])
            await session.flush()
            return await self._snapshot(session, loop)

    async def get(self, loop_id: str) -> dict:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, loop_id)
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            return await self._snapshot(session, loop)

    async def active_for_context(self, context_id: str) -> dict | None:
        async with self._sessions() as session:
            loop = await session.scalar(
                select(AgentLoop)
                .join(LoopContextMembership, LoopContextMembership.loop_id == AgentLoop.loop_id)
                .where(
                    LoopContextMembership.context_id == context_id,
                    LoopContextMembership.status.in_(["active", "paused"]),
                    AgentLoop.status.in_(["running", "pausing", "paused", "waiting_user", "completing"]),
                )
                .order_by(AgentLoop.created_at.desc())
                .limit(1)
            )
            return None if loop is None else await self._snapshot(session, loop)

    async def user_message(self, context_id: str, content: str) -> dict | None:
        async with self._sessions.begin() as session:
            loop = await session.scalar(select(AgentLoop).join(LoopContextMembership, LoopContextMembership.loop_id == AgentLoop.loop_id).where(LoopContextMembership.context_id == context_id, LoopContextMembership.status.in_(["active", "paused"]), AgentLoop.status.in_(["running", "paused", "waiting_user"])).order_by(AgentLoop.created_at.desc()).with_for_update().limit(1))
            if loop is None:
                return None
            current_goal = await session.scalar(select(LoopGoalRevision).where(LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == loop.goal_revision))
            current_grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active").with_for_update())
            if current_goal is None or current_grant is None:
                raise HTTPException(409, "Agent Loop 缺少当前 goal 或 grant")
            loop.revision += 1
            loop.goal_revision += 1
            loop.authority_revision += 1
            loop.status = "running"
            loop.health = "waiting_runs"
            loop.waiting_reason = None
            current_grant.status = "revoked"
            current_grant.revoked_at = datetime.now(UTC)
            grant = LoopDelegationGrant(grant_id=uuid.uuid4().hex, loop_id=loop.loop_id, revision=loop.authority_revision, holder_id=current_grant.holder_id, capabilities=current_grant.capabilities, context_scope=current_grant.context_scope, permission_scope=current_grant.permission_scope, budgets=current_grant.budgets, delegable_gates=current_grant.delegable_gates, expires_at=current_grant.expires_at)
            goal = LoopGoalRevision(goal_revision_id=uuid.uuid4().hex, loop_id=loop.loop_id, revision=loop.goal_revision, goal=current_goal.goal, task_contract=f"{current_goal.task_contract}\n\n用户最新直接指令：\n{content}", acceptance_criteria=current_goal.acceptance_criteria, authored_by="user")
            await session.execute(update(LoopRound).where(LoopRound.loop_id == loop.loop_id, LoopRound.status.in_(["observed", "ready", "waiting_workers", "publishing", "adopting"])).values(status="superseded"))
            await session.execute(update(LoopDecision).where(LoopDecision.loop_id == loop.loop_id, LoopDecision.status.in_(["pending", "publishing", "adopting"])).values(status="superseded"))
            await session.execute(update(LoopDirective).where(LoopDirective.loop_id == loop.loop_id, LoopDirective.status.in_(["created", "launching"])).values(status="cancelled"))
            active_runs = list(
                (
                    await session.scalars(
                        select(DesktopRun).where(
                            DesktopRun.loop_id == loop.loop_id,
                            DesktopRun.status.in_(["pending", "running"]),
                        ).with_for_update()
                    )
                ).all()
            )
            for run in active_runs:
                if self._run_manager is not None:
                    self._run_manager.cancel(run.run_id, action="interrupt")
                run.status = "interrupted"
                lease_id = (run.workspace_anchor or {}).get("lease_id")
                if lease_id:
                    lease = await session.get(WorkspaceLease, lease_id, with_for_update=True)
                    if lease is not None and lease.status == "active":
                        lease.status = "released"
                        lease.released_at = datetime.now(UTC)
            prior = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
            slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == loop.workspace_id, WorkspaceSlot.kind == "authoritative", WorkspaceSlot.lifecycle != "deleted"))
            number = int(await session.scalar(select(func.max(LoopRound.number)).where(LoopRound.loop_id == loop.loop_id)) or 0) + 1
            round_row = LoopRound(round_id=uuid.uuid4().hex, loop_id=loop.loop_id, number=number, status="running", authority_revision=loop.authority_revision, goal_revision=loop.goal_revision, frontier_hash=prior.frontier_hash if prior else self._hash({"context_id": context_id}), workspace_revision=slot.revision if slot else 1)
            loop.current_round_id = round_row.round_id
            session.add_all([grant, goal, round_row])
            await self._append_event(session, loop, "DirectUserMessage", {"round_id": round_row.round_id, "goal_revision": loop.goal_revision})
            return {"loop_id": loop.loop_id, "round_id": round_row.round_id, "goal_revision": loop.goal_revision}

    async def control(self, loop_id: str, command: str) -> dict:
        transitions = {"pause": ({"running"}, "paused"), "resume": ({"paused", "waiting_user"}, "running"), "stop": ({"running", "paused", "waiting_user"}, "stopped")}
        if command not in transitions:
            raise HTTPException(422, "未知 Loop 控制命令")
        async with self._sessions.begin() as session:
            loop = await session.scalar(select(AgentLoop).where(AgentLoop.loop_id == loop_id).with_for_update())
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            allowed, target = transitions[command]
            if loop.status not in allowed:
                raise HTTPException(409, f"Loop 状态 {loop.status} 不允许 {command}")
            loop.status = target
            loop.health = "idle" if target != "running" else "observing"
            loop.revision += 1
            if target == "stopped":
                loop.completed_at = datetime.now(UTC)
                await self._revoke(session, loop)
                await session.execute(
                    update(LoopDirective)
                    .where(LoopDirective.loop_id == loop_id, LoopDirective.status.in_(["created", "launching"]))
                    .values(status="cancelled")
                )
                run_ids = list(
                    (
                        await session.scalars(
                            select(DesktopRun.run_id).where(
                                DesktopRun.loop_id == loop_id,
                                DesktopRun.status.in_(["pending", "running"]),
                            )
                        )
                    ).all()
                )
                if self._run_manager is not None:
                    for run_id in run_ids:
                        self._run_manager.cancel(run_id, action="interrupt")
            await self._append_event(session, loop, f"Loop{command.title()}", {"status": target})
            return await self._snapshot(session, loop)

    async def fail_user_message_round(self, loop_id: str, round_id: str, reason: str) -> None:
        async with self._sessions.begin() as session:
            loop = await session.get(AgentLoop, loop_id, with_for_update=True)
            round_row = await session.get(LoopRound, round_id, with_for_update=True)
            if loop is None or round_row is None or round_row.loop_id != loop_id:
                return
            if round_row.status == "running":
                round_row.status = "error"
            if loop.current_round_id == round_id and loop.status == "running":
                loop.status = "waiting_user"
                loop.health = "degraded"
                loop.waiting_reason = f"用户 Run 准备失败: {reason[:1000]}"
                await self._append_event(
                    session,
                    loop,
                    "DirectUserRunPreparationFailed",
                    {"round_id": round_id, "reason": reason[:1000]},
                )

    async def override(self, loop_id: str, goal: str, task_contract: str, acceptance_criteria: list[dict]) -> dict:
        if not acceptance_criteria:
            raise HTTPException(422, "用户覆盖必须提供验收条件")
        async with self._sessions.begin() as session:
            loop = await session.scalar(select(AgentLoop).where(AgentLoop.loop_id == loop_id).with_for_update())
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            loop.revision += 1
            loop.goal_revision += 1
            loop.authority_revision += 1
            loop.status = "running"
            loop.health = "observing"
            await session.execute(update(LoopDecision).where(LoopDecision.loop_id == loop_id, LoopDecision.status.in_(["pending", "publishing", "adopting"])).values(status="superseded"))
            await session.execute(update(LoopRound).where(LoopRound.loop_id == loop_id, LoopRound.status.in_(["observed", "ready", "waiting_workers", "publishing", "adopting"])).values(status="superseded"))
            await session.execute(update(LoopDirective).where(LoopDirective.loop_id == loop_id, LoopDirective.status.in_(["created", "launching"])).values(status="cancelled"))
            old_grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active").with_for_update())
            if old_grant is not None:
                old_grant.status = "revoked"
                old_grant.revoked_at = datetime.now(UTC)
                session.add(LoopDelegationGrant(grant_id=uuid.uuid4().hex, loop_id=loop_id, revision=loop.authority_revision, holder_id=old_grant.holder_id, capabilities=old_grant.capabilities, context_scope=old_grant.context_scope, permission_scope=old_grant.permission_scope, budgets=old_grant.budgets, delegable_gates=old_grant.delegable_gates, expires_at=old_grant.expires_at))
            session.add(LoopGoalRevision(goal_revision_id=uuid.uuid4().hex, loop_id=loop_id, revision=loop.goal_revision, goal=goal, task_contract=task_contract, acceptance_criteria=acceptance_criteria, authored_by="user"))
            number = int(await session.scalar(select(func.max(LoopRound.number)).where(LoopRound.loop_id == loop_id)) or 0) + 1
            prior = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
            slot = await session.scalar(
                select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == loop.workspace_id,
                    WorkspaceSlot.kind == "authoritative",
                    WorkspaceSlot.lifecycle != "deleted",
                )
            )
            round_row = LoopRound(
                round_id=uuid.uuid4().hex,
                loop_id=loop_id,
                number=number,
                authority_revision=loop.authority_revision,
                goal_revision=loop.goal_revision,
                frontier_hash=prior.frontier_hash if prior else self._hash({"initial_context_id": loop.initial_context_id}),
                workspace_revision=slot.revision if slot else 1,
            )
            loop.current_round_id = round_row.round_id
            session.add(round_row)
            await self._append_event(session, loop, "UserOverride", {"round_id": round_row.round_id, "goal_revision": loop.goal_revision})
            return await self._snapshot(session, loop)

    async def events(self, loop_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        async with self._sessions() as session:
            rows = list((await session.scalars(select(LoopEventOutbox).where(LoopEventOutbox.loop_id == loop_id, LoopEventOutbox.sequence > after).order_by(LoopEventOutbox.sequence).limit(limit))).all())
            return [{"event_id": row.event_id, "cursor": row.sequence, "type": row.event_type, "payload": row.payload, "created_at": row.created_at.isoformat()} for row in rows]

    async def _snapshot(self, session, loop: AgentLoop) -> dict:
        goal = await session.scalar(select(LoopGoalRevision).where(LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == loop.goal_revision))
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == loop.authority_revision))
        usage = await session.get(LoopBudgetUsage, loop.loop_id)
        context_count = int(
            await session.scalar(
                select(func.count()).select_from(LoopContextMembership).where(
                    LoopContextMembership.loop_id == loop.loop_id,
                    LoopContextMembership.status != "discarded",
                )
            )
            or 0
        )
        return {"loop_id": loop.loop_id, "workspace_id": loop.workspace_id, "initial_context_id": loop.initial_context_id, "program_id": loop.program_id, "status": loop.status, "health": loop.health, "revision": loop.revision, "goal_revision": loop.goal_revision, "authority_revision": loop.authority_revision, "holder_id": loop.holder_id, "current_round_id": loop.current_round_id, "current_portfolio_revision_id": loop.current_portfolio_revision_id, "waiting_reason": loop.waiting_reason, "equipment": loop.equipment, "goal": None if goal is None else {"goal": goal.goal, "task_contract": goal.task_contract, "acceptance_criteria": goal.acceptance_criteria}, "grant": None if grant is None else {"grant_id": grant.grant_id, "status": grant.status, "capabilities": grant.capabilities, "context_scope": grant.context_scope, "permission_scope": grant.permission_scope, "budgets": grant.budgets}, "usage": None if usage is None else {"rounds": usage.rounds, "model_calls": usage.model_calls, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens, "retries": usage.retries, "lanes": usage.lanes, "contexts": context_count, "providers": configured_provider_count(loop.equipment or {}), "no_progress_count": usage.no_progress_count}, "final_result": loop.final_result}

    @staticmethod
    async def _revoke(session, loop) -> None:
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active").with_for_update())
        if grant is not None:
            grant.status = "revoked"
            grant.revoked_at = datetime.now(UTC)

    @staticmethod
    async def _append_event(session, loop, event_type, payload) -> None:
        sequence = int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop.loop_id)) or 0) + 1
        session.add(LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop.loop_id, sequence=sequence, event_type=event_type, payload=payload, idempotency_key=f"{loop.loop_id}:{loop.revision}:{event_type}"))

    @staticmethod
    def _hash(value) -> str:
        return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()
