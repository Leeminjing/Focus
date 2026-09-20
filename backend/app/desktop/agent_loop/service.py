r"""本文件对外提供 AgentLoopService 创建、查询、Mission 修订、类型化等待响应、控制、临时用户介入与事件读取用例。

输入为认证后的 LoopCreateRequest、Loop id、控制动作或用户确认的 Mission；输出为完整 Loop 快照与
持久事件。具体工作流为 start 原子绑定初始用户 Run 并双写兼容 Goal 与结构化 Mission，再创建 grant/holder/membership/budget/首轮，pause/resume/stop
推进 revision，并由 RuntimeConvergence 原子收敛未提交 round、Worker、directive 和活动 Run；失败轮恢复时新建观察轮并累计 retry；直接用户消息只建立一次性 intent 并以 authority revision
隔离旧 Patrol 工作，不改写 Mission；override 仅在用户确认后创建 Mission revision。示例：`await service.start(body)`。
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

from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopContextMembership, LoopDecision, LoopDelegationGrant, LoopDirective, LoopEventOutbox, LoopGoalRevision, LoopPendingDecision, LoopRound, LoopUserIntent
from backend.app.desktop.agent_loop.schemas import LoopCreateRequest, LoopWaitResponseRequest
from backend.app.desktop.agent_loop.mission_contract import LegacyMissionAdapter, LoopMissionContract
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_service import MissionRevisionService
from backend.app.desktop.agent_loop.compression_authority.repository import CompressionAuthorityRepository
from backend.app.desktop.agent_loop.rounds import create_observation_round
from backend.app.desktop.agent_loop.runtime_convergence import LoopRuntimeConvergence
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.context_curation.models import CurationLane, CurationProgram, CurationSourceSubscription, PortfolioLaneCandidate, PortfolioRevision
from backend.app.desktop.context_evolution.models import ContextRevision
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.workspace_coordination.fingerprints import WorkspaceFingerprinter
from backend.app.desktop.workspace_coordination.models import WorkspaceLease, WorkspaceSlot
from backend.app.desktop.agent_loop.budgets import configured_provider_count
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest
from backend.app.desktop.agent_loop.wait_requests import LoopWaitRequestFactory, LoopWaitRequestService, WaitRequestConflict
from backend.app.desktop.agent_loop.activation_eligibility import LoopActivationEligibilityResolver
from backend.app.desktop.agent_loop.activation_models import LoopActivation


class AgentLoopService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], run_manager=None) -> None:
        self._sessions = sessions
        self._run_manager = run_manager
        self._missions = MissionRevisionService()
        self._convergence = LoopRuntimeConvergence()
        self._directives = DirectiveLifecycleRepository()
        self._interventions = InterventionLifecycleRepository()
        self._journal = LoopEventJournal()
        self._waits = LoopWaitRequestService()
        self._activation = LoopActivationEligibilityResolver()

    async def start(self, request: LoopCreateRequest) -> dict:
        if "request_completion" not in request.capabilities:
            raise HTTPException(422, "无人值守 Loop 必须明确是否允许 Patrol 请求完成")
        compression_enabled = "compression" in request.delegable_gates
        compression_policy = (
            request.compression_policy.model_dump(mode="json")
            if compression_enabled and request.compression_policy is not None and "apply_context_compression" in request.capabilities
            else {}
        )
        mission = request.resolved_mission()
        legacy_mission = LegacyMissionAdapter.export(mission)
        async with self._sessions.begin() as session:
            existing = await session.get(AgentLoop, request.loop_id)
            if existing is not None:
                return await self._snapshot(session, existing)
            activation_key = request.activation_key or f"loop-activation:{request.initial_run_id}"
            await session.execute(select(func.pg_advisory_xact_lock(self._activation_lock_key(request.initial_run_id))))
            existing_activation = await session.scalar(
                select(LoopActivation).where(
                    (LoopActivation.activation_key == activation_key)
                    | (LoopActivation.selected_run_id == request.initial_run_id)
                ).with_for_update()
            )
            if existing_activation is not None:
                existing_loop = await session.get(AgentLoop, existing_activation.loop_id)
                if existing_loop is None:
                    raise HTTPException(409, "Loop activation lineage 指向不存在的 Loop")
                return await self._snapshot(session, existing_loop)
            context = await session.get(DesktopThread, request.initial_context_id)
            workspace = await session.get(DesktopWorkspace, request.workspace_id)
            if context is None or workspace is None or context.workspace_id != workspace.workspace_id:
                raise HTTPException(422, "初始 Context 与 workspace 不匹配")
            if context.current_revision_id is None:
                raise HTTPException(409, "初始 Context 尚无可执行 revision")
            context_revision = await session.get(ContextRevision, context.current_revision_id)
            if context_revision is None:
                raise HTTPException(409, "初始 Context revision 不存在")
            eligibility = await self._activation.resolve(session, request.initial_context_id, lock=True)
            if request.readiness_token is not None and request.readiness_token != eligibility.consistency_token:
                raise HTTPException(409, {"code": "stale_loop_readiness", "eligibility": self._activation_payload(eligibility)})
            if not eligibility.eligible or eligibility.candidate_run_id != request.initial_run_id:
                raise HTTPException(409, {"code": eligibility.reason or "initial_run_changed", "eligibility": self._activation_payload(eligibility)})
            initial_run = await self._initial_run(session, request)
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
            session.add(
                LoopActivation(
                    activation_id=uuid.uuid4().hex,
                    loop_id=loop.loop_id,
                    selected_run_id=initial_run.run_id,
                    predecessor_loop_id=eligibility.predecessor_loop_id,
                    readiness_token=eligibility.consistency_token,
                    activation_key=activation_key,
                )
            )
            goal = LoopGoalRevision(goal_revision_id=uuid.uuid4().hex, loop_id=loop.loop_id, revision=1, goal=legacy_mission["goal"], task_contract=legacy_mission["task_contract"], acceptance_criteria=legacy_mission["acceptance_criteria"], authored_by="user")
            session.add(goal)
            await session.flush()
            await self._missions.record(
                session,
                loop_id=loop.loop_id,
                revision=1,
                contract=mission,
                authored_by="user",
                legacy_goal_revision_id=goal.goal_revision_id,
            )
            grant = LoopDelegationGrant(grant_id=uuid.uuid4().hex, loop_id=loop.loop_id, revision=1, holder_id=request.holder_id, capabilities=list(request.capabilities), context_scope=list(request.context_scope), permission_scope=list(request.permission_scope), budgets=request.budgets.model_dump(), delegable_gates=list(request.delegable_gates), compression_policy=compression_policy, expires_at=datetime.fromisoformat(request.expires_at) if request.expires_at else None)
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
            active_initial_run = initial_run.status in {"pending", "running"}
            round_row = LoopRound(round_id=uuid.uuid4().hex, loop_id=loop.loop_id, number=1, status="running" if active_initial_run else "settled", authority_revision=1, goal_revision=1, frontier_hash=frontier_hash, workspace_revision=slot.revision, settled_at=None if active_initial_run else datetime.now(UTC))
            initial_run.loop_id = loop.loop_id
            initial_run.round_id = round_row.round_id
            usage = LoopBudgetUsage(
                loop_id=loop.loop_id,
                rounds=0 if active_initial_run else 1,
                model_calls=0 if active_initial_run else int(initial_run.model_call_count or 0),
                input_tokens=0 if active_initial_run else int(initial_run.prompt_input_tokens or 0),
                output_tokens=0 if active_initial_run else int(initial_run.prompt_output_tokens or 0),
            )
            current_round = round_row
            if active_initial_run:
                loop.health = "waiting_runs"
            else:
                current_round = LoopRound(round_id=uuid.uuid4().hex, loop_id=loop.loop_id, number=2, authority_revision=1, goal_revision=1, frontier_hash=frontier_hash, workspace_revision=slot.revision)
            loop.current_round_id = current_round.round_id
            rows = [grant, candidate, membership, round_row, usage, LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop.loop_id, sequence=1, event_type="LoopStarted", payload={"round_id": round_row.round_id, "current_round_id": current_round.round_id, "initial_run_id": initial_run.run_id, "program_id": program_id, "portfolio_revision_id": portfolio_id}, idempotency_key=f"loop:{loop.loop_id}:started")]
            if current_round is not round_row:
                rows.append(current_round)
            session.add_all(rows)
            await session.flush()
            if not active_initial_run:
                await self._journal.append(
                    session,
                    loop.loop_id,
                    CanonicalEventDraft(
                        kind="context.run.settled",
                        entity_type="context_run",
                        entity_id=initial_run.run_id,
                        entity_revision=2,
                        correlation_id=initial_run.user_intent_id or f"initial-run:{initial_run.run_id}",
                        payload={"run_id": initial_run.run_id, "context_id": initial_run.task_id, "status": initial_run.status, "initial": True},
                        idempotency_key=f"context-run:{initial_run.run_id}:settled",
                    ),
                )
            return await self._snapshot(session, loop)

    async def get(self, loop_id: str) -> dict:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, loop_id)
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            return await self._snapshot(session, loop)

    async def active_wait_request(self, loop_id: str) -> dict | None:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, loop_id)
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            request = await self._waits.active(session, loop_id)
            return None if request is None else self._wait_request_payload(request)

    async def resolve_wait_request(
        self,
        loop_id: str,
        request_id: str,
        body: LoopWaitResponseRequest,
        *,
        actor_id: str,
    ) -> dict:
        async with self._sessions.begin() as session:
            request = await session.get(LoopWaitRequest, request_id)
            if request is None or request.loop_id != loop_id:
                raise HTTPException(404, "等待请求不存在")
            self._validate_wait_answer(request, body.answer)
            try:
                request, response, created = await self._waits.resolve(
                    session,
                    request_id,
                    answer=body.answer,
                    actor_id=actor_id,
                    request_revision=body.request_revision,
                    idempotency_key=body.idempotency_key,
                )
            except WaitRequestConflict as exc:
                detail = {"code": "wait_request_conflict", "message": str(exc)}
                if exc.committed is not None:
                    detail["committed_response_id"] = exc.committed.response_id
                    detail["committed_answer"] = exc.committed.answer
                raise HTTPException(409, detail) from exc
            loop = await session.get(AgentLoop, loop_id, with_for_update=True)
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            action = str(body.answer.get("action") or "")
            if action == "stop":
                loop.status = "stopped"
                loop.health = "idle"
                loop.completed_at = datetime.now(UTC)
                await self._revoke(session, loop)
                await self._convergence.converge(session, loop, "wait_request_stop")
            elif action == "revise_budget":
                await self._apply_wait_budget_revision(session, loop, body.answer)
            if created:
                if loop.status == "running":
                    await self._resume_after_wait(session, loop, request, response.response_id)
                await self._append_wait_transition_event(
                    session,
                    loop,
                    request,
                    response.response_id,
                    terminated=loop.status == "stopped",
                )
                await self._append_event(session, loop, "LoopWaitResponseCommitted", {"request_id": request.request_id, "response_id": response.response_id, "status": loop.status})
            return {"request": self._wait_request_payload(request), "response_id": response.response_id, "created": created, "loop_status": loop.status}

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

    async def activation_eligibility(self, context_id: str) -> dict:
        async with self._sessions() as session:
            context = await session.get(DesktopThread, context_id)
            if context is None:
                raise HTTPException(404, "Context 不存在")
            return self._activation_payload(await self._activation.resolve(session, context_id))

    async def user_resolved_compression_gate(self, thread_id: str) -> None:
        async with self._sessions.begin() as session:
            loop = await session.scalar(
                select(AgentLoop)
                .join(LoopContextMembership, LoopContextMembership.loop_id == AgentLoop.loop_id)
                .join(DesktopThread, DesktopThread.task_id == LoopContextMembership.context_id)
                .where(
                    DesktopThread.thread_id == thread_id,
                    AgentLoop.status.in_(["running", "paused", "waiting_user"]),
                )
                .order_by(AgentLoop.created_at.desc())
                .with_for_update()
                .limit(1)
            )
            if loop is None:
                return
            await self._cancel_autonomous_compression_runs(session, loop.loop_id)
            await CompressionAuthorityRepository().supersede(session, loop.loop_id, "manual_compression_resolution")
            pending = list(
                (
                    await session.scalars(
                        select(LoopPendingDecision)
                        .where(
                            LoopPendingDecision.loop_id == loop.loop_id,
                            LoopPendingDecision.kind == "compression",
                            LoopPendingDecision.status.in_(["pending", "resolving"]),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for row in pending:
                row.status = "superseded"

    async def user_message(self, context_id: str, content: str) -> dict | None:
        async with self._sessions.begin() as session:
            loop = await session.scalar(select(AgentLoop).join(LoopContextMembership, LoopContextMembership.loop_id == AgentLoop.loop_id).where(LoopContextMembership.context_id == context_id, LoopContextMembership.status.in_(["active", "paused"]), AgentLoop.status.in_(["running", "paused"])).order_by(AgentLoop.created_at.desc()).with_for_update().limit(1))
            if loop is None:
                return None
            current_grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active").with_for_update())
            if current_grant is None:
                return None
            loop.revision += 1
            loop.authority_revision += 1
            loop.status = "running"
            loop.health = "waiting_runs"
            loop.waiting_reason = None
            current_grant.status = "revoked"
            current_grant.revoked_at = datetime.now(UTC)
            grant = LoopDelegationGrant(grant_id=uuid.uuid4().hex, loop_id=loop.loop_id, revision=loop.authority_revision, holder_id=current_grant.holder_id, capabilities=current_grant.capabilities, context_scope=current_grant.context_scope, permission_scope=current_grant.permission_scope, budgets=current_grant.budgets, delegable_gates=current_grant.delegable_gates, compression_policy=current_grant.compression_policy, expires_at=current_grant.expires_at)
            intent_id = uuid.uuid4().hex
            intent = LoopUserIntent(intent_id=intent_id, loop_id=loop.loop_id, scope="context", target_context_id=context_id, content=content, status="addressed", goal_revision=loop.goal_revision, authority_revision=loop.authority_revision, correlation_id=intent_id)
            await session.execute(update(LoopRound).where(LoopRound.loop_id == loop.loop_id, LoopRound.status.in_(["observed", "curated", "ready", "waiting_workers", "publishing", "adopting"])).values(status="superseded"))
            await session.execute(update(LoopDecision).where(LoopDecision.loop_id == loop.loop_id, LoopDecision.status.in_(["pending", "publishing", "adopting"])).values(status="superseded"))
            await self._directives.cancel_active(session, loop.loop_id, "direct_user_message")
            await CompressionAuthorityRepository().supersede(session, loop.loop_id, "direct_user_message")
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
            session.add_all([grant, intent, round_row])
            await self._interventions.register(session, intent)
            await self._interventions.transition(session, intent.intent_id, "accepted")
            await self._append_event(session, loop, "DirectUserMessage", {"intent_id": intent.intent_id, "context_id": context_id, "round_id": round_row.round_id, "mission_revision": loop.goal_revision})
            return {"loop_id": loop.loop_id, "round_id": round_row.round_id, "intent_id": intent.intent_id, "goal_revision": loop.goal_revision, "mission_revision": loop.goal_revision}

    async def bind_user_message_run(self, intent_id: str, run_id: str) -> None:
        async with self._sessions.begin() as session:
            intent = await session.get(LoopUserIntent, intent_id, with_for_update=True)
            if intent is None or intent.origin_kind != "user" or intent.delivery_state != "accepted":
                raise LookupError("直接用户消息 intent 已失效")
            await self._interventions.transition(session, intent_id, "delivered", run_id=run_id)
            await self._interventions.transition(session, intent_id, "run_started", run_id=run_id)

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
            if command == "resume" and loop.status == "waiting_user":
                active_wait = await self._waits.active(session, loop_id, lock=True)
                raise HTTPException(409, {"code": "wait_response_required", "request_id": active_wait.request_id if active_wait else None})
            if command == "resume":
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active"))
                if grant is None:
                    raise HTTPException(409, "Patrol delegation 已撤销，不能恢复无人值守 Loop")
                current = await session.get(LoopRound, loop.current_round_id, with_for_update=True) if loop.current_round_id else None
                if current is None or current.status in {"settled", "superseded", "error"}:
                    resumed = await create_observation_round(
                        session,
                        loop,
                        current,
                        self._hash({"initial_context_id": loop.initial_context_id}),
                    )
                    session.add(resumed)
                    loop.current_round_id = resumed.round_id
                    if current is not None and current.status == "error":
                        usage = await session.get(LoopBudgetUsage, loop.loop_id, with_for_update=True)
                        if usage is not None:
                            LoopUsageLedger.apply(usage, LoopUsageDelta(retries=1))
            loop.status = target
            loop.health = "idle" if target != "running" else "observing"
            loop.revision += 1
            if command == "pause":
                await self._cancel_autonomous_compression_runs(session, loop_id)
                await CompressionAuthorityRepository().supersede(session, loop_id, "loop_paused")
                run_ids = await self._convergence.converge(session, loop, "loop_paused")
            if target == "stopped":
                active_wait = await self._waits.active(session, loop_id, lock=True)
                if active_wait is not None:
                    await self._waits.cancel(session, active_wait.request_id)
                loop.completed_at = datetime.now(UTC)
                await self._revoke(session, loop)
                await CompressionAuthorityRepository().supersede(session, loop_id, "loop_stopped")
                run_ids = await self._convergence.converge(session, loop, "loop_stopped")
            if command in {"pause", "stop"} and self._run_manager is not None:
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
            intent = await session.scalar(
                select(LoopUserIntent)
                .where(
                    LoopUserIntent.loop_id == loop_id,
                    LoopUserIntent.delivery_state.in_(("accepted", "delivered")),
                )
                .order_by(LoopUserIntent.created_at.desc())
                .with_for_update()
                .limit(1)
            )
            if intent is not None:
                target = "delivery_failed" if intent.delivery_state == "delivered" else "rejected"
                await self._interventions.transition(session, intent.intent_id, target, reason=reason)
            if round_row.status == "running":
                round_row.status = "error"
            if loop.current_round_id == round_id and loop.status == "running":
                loop.health = "degraded"
                await self._waits.open(
                    session,
                    loop,
                    LoopWaitRequestFactory.retry_or_stop(
                        f"用户 Run 准备失败: {reason[:1000]}",
                        {"round_id": round_id, "failure_kind": "run_preparation"},
                    ),
                    created_by="run-preparation",
                    correlation_id=intent.correlation_id if intent is not None else f"round:{round_id}",
                    round_id=round_id,
                )
                await self._append_event(
                    session,
                    loop,
                    "DirectUserRunPreparationFailed",
                    {"round_id": round_id, "reason": reason[:1000]},
                )

    async def override(
        self,
        loop_id: str,
        goal: str | None = None,
        task_contract: str | None = None,
        acceptance_criteria: list[dict] | None = None,
        *,
        mission: LoopMissionContract | None = None,
    ) -> dict:
        resolved_mission = mission or LegacyMissionAdapter.convert(
            goal=goal or "",
            task_contract=task_contract or "",
            acceptance_criteria=acceptance_criteria or (),
        )
        legacy_mission = LegacyMissionAdapter.export(resolved_mission)
        async with self._sessions.begin() as session:
            loop = await session.scalar(select(AgentLoop).where(AgentLoop.loop_id == loop_id).with_for_update())
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            loop.revision += 1
            loop.goal_revision += 1
            loop.authority_revision += 1
            loop.status = "running"
            loop.health = "observing"
            active_wait = await self._waits.active(session, loop_id, lock=True)
            if active_wait is not None:
                await self._waits.cancel(session, active_wait.request_id, superseded=True)
            await session.execute(update(LoopDecision).where(LoopDecision.loop_id == loop_id, LoopDecision.status.in_(["pending", "publishing", "adopting"])).values(status="superseded"))
            await session.execute(update(LoopRound).where(LoopRound.loop_id == loop_id, LoopRound.status.in_(["observed", "curated", "ready", "waiting_workers", "publishing", "adopting"])).values(status="superseded"))
            await self._directives.cancel_active(session, loop_id, "mission_revision")
            old_grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active").with_for_update())
            if old_grant is not None:
                old_grant.status = "revoked"
                old_grant.revoked_at = datetime.now(UTC)
                session.add(LoopDelegationGrant(grant_id=uuid.uuid4().hex, loop_id=loop_id, revision=loop.authority_revision, holder_id=old_grant.holder_id, capabilities=old_grant.capabilities, context_scope=old_grant.context_scope, permission_scope=old_grant.permission_scope, budgets=old_grant.budgets, delegable_gates=old_grant.delegable_gates, compression_policy=old_grant.compression_policy, expires_at=old_grant.expires_at))
            await self._cancel_autonomous_compression_runs(session, loop_id)
            await CompressionAuthorityRepository().supersede(session, loop_id, "user_override")
            goal_row = LoopGoalRevision(goal_revision_id=uuid.uuid4().hex, loop_id=loop_id, revision=loop.goal_revision, goal=legacy_mission["goal"], task_contract=legacy_mission["task_contract"], acceptance_criteria=legacy_mission["acceptance_criteria"], authored_by="user")
            session.add(goal_row)
            await session.flush()
            mission_row = await self._missions.record(
                session,
                loop_id=loop_id,
                revision=loop.goal_revision,
                contract=resolved_mission,
                authored_by="user",
                legacy_goal_revision_id=goal_row.goal_revision_id,
            )
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
            await self._append_event(session, loop, "MissionRevisionActivated", {"round_id": round_row.round_id, "mission_revision": loop.goal_revision, "mission_revision_id": mission_row.mission_revision_id, "previous_mission_revision": loop.goal_revision - 1})
            return await self._snapshot(session, loop)

    async def events(self, loop_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        async with self._sessions() as session:
            rows = list((await session.scalars(select(LoopEventOutbox).where(LoopEventOutbox.loop_id == loop_id, LoopEventOutbox.sequence > after).order_by(LoopEventOutbox.sequence).limit(limit))).all())
            return [{"event_id": row.event_id, "cursor": row.sequence, "type": row.event_type, "payload": row.payload, "created_at": row.created_at.isoformat()} for row in rows]

    async def _cancel_autonomous_compression_runs(self, session: AsyncSession, loop_id: str) -> tuple[str, ...]:
        runs = tuple(
            (
                await session.scalars(
                    select(DesktopRun)
                    .where(
                        DesktopRun.loop_id == loop_id,
                        DesktopRun.origin == "delegated_patrol_compression",
                        DesktopRun.status.in_(["pending", "running"]),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for run in runs:
            if self._run_manager is not None:
                self._run_manager.cancel(run.run_id, action="interrupt")
            run.status = "interrupted"
            lease_id = (run.workspace_anchor or {}).get("lease_id")
            if lease_id:
                lease = await session.get(WorkspaceLease, lease_id, with_for_update=True)
                if lease is not None and lease.status == "active":
                    lease.status = "released"
                    lease.released_at = datetime.now(UTC)
        return tuple(run.run_id for run in runs)

    @staticmethod
    async def _initial_run(session: AsyncSession, request: LoopCreateRequest) -> DesktopRun:
        run = await session.get(DesktopRun, request.initial_run_id, with_for_update=True)
        if run is None:
            raise HTTPException(422, "初始用户 Run 不存在")
        if (
            run.task_id != request.initial_context_id
            or run.agent_id != f"main:{request.initial_context_id}"
            or run.kind != "main"
            or run.origin != "direct_user"
        ):
            raise HTTPException(422, "初始 Run 必须是当前 Context 的直接用户 Main Run")
        latest_run_id = await session.scalar(
            select(DesktopRun.run_id)
            .where(
                DesktopRun.task_id == request.initial_context_id,
                DesktopRun.agent_id == f"main:{request.initial_context_id}",
                DesktopRun.kind == "main",
                DesktopRun.origin == "direct_user",
            )
            .order_by(DesktopRun.created_at.desc(), DesktopRun.run_id.desc())
            .limit(1)
        )
        if latest_run_id != run.run_id:
            raise HTTPException(409, "初始 Run 必须是当前 Context 最新的直接用户 Main Run")
        if run.loop_id is not None:
            raise HTTPException(409, "初始 Run 已属于另一个 Agent Loop")
        if run.status not in {"pending", "running", "success", "error", "interrupted"}:
            raise HTTPException(409, f"初始 Run 状态 {run.status} 不可绑定")
        return run

    async def _snapshot(self, session, loop: AgentLoop) -> dict:
        goal = await session.scalar(select(LoopGoalRevision).where(LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == loop.goal_revision))
        mission = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == loop.loop_id, LoopMissionRevision.revision == loop.goal_revision))
        grant = await session.scalar(
            select(LoopDelegationGrant).where(
                LoopDelegationGrant.loop_id == loop.loop_id,
                LoopDelegationGrant.revision == loop.authority_revision,
                LoopDelegationGrant.status == "active",
            )
        )
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
        created_at = loop.created_at if loop.created_at.tzinfo else loop.created_at.replace(tzinfo=UTC)
        duration_seconds = max(0, int((datetime.now(UTC) - created_at).total_seconds()))
        mission_payload = None if mission is None else {"mission_revision_id": mission.mission_revision_id, "revision": mission.revision, "active": True, "outcome": mission.outcome, "boundaries": mission.boundaries, "completion_checks": mission.completion_checks, "authored_by": mission.authored_by, "legacy_goal_revision_id": mission.legacy_goal_revision_id}
        wait_request = await self._waits.active(session, loop.loop_id)
        return {"loop_id": loop.loop_id, "workspace_id": loop.workspace_id, "initial_context_id": loop.initial_context_id, "program_id": loop.program_id, "status": loop.status, "health": loop.health, "revision": loop.revision, "goal_revision": loop.goal_revision, "active_mission_revision": loop.goal_revision, "authority_revision": loop.authority_revision, "holder_id": loop.holder_id, "current_round_id": loop.current_round_id, "current_portfolio_revision_id": loop.current_portfolio_revision_id, "waiting_reason": loop.waiting_reason, "wait_request": None if wait_request is None else self._wait_request_payload(wait_request), "equipment": loop.equipment, "mission": mission_payload, "goal": None if goal is None else {"goal": goal.goal, "task_contract": goal.task_contract, "acceptance_criteria": goal.acceptance_criteria}, "grant": None if grant is None else {"grant_id": grant.grant_id, "status": grant.status, "capabilities": grant.capabilities, "context_scope": grant.context_scope, "permission_scope": grant.permission_scope, "budgets": grant.budgets, "delegable_gates": grant.delegable_gates, "compression_policy": grant.compression_policy, "expires_at": grant.expires_at.isoformat() if grant.expires_at else None}, "usage": None if usage is None else {"rounds": usage.rounds, "duration_seconds": duration_seconds, "model_calls": usage.model_calls, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens, "retries": usage.retries, "lanes": usage.lanes, "contexts": context_count, "providers": configured_provider_count(loop.equipment or {}), "no_progress_count": usage.no_progress_count}, "final_result": loop.final_result}

    @staticmethod
    def _wait_request_payload(request: LoopWaitRequest) -> dict:
        return {"request_id": request.request_id, "loop_id": request.loop_id, "round_id": request.round_id, "kind": request.kind, "prompt": request.prompt, "response_mode": request.response_mode, "response_contract": request.response_contract, "scope": request.scope, "status": request.status, "revision": request.revision, "correlation_id": request.correlation_id, "causation_id": request.causation_id, "created_by": request.created_by, "created_at": request.created_at.isoformat() if request.created_at else None}

    @staticmethod
    def _activation_payload(eligibility) -> dict:
        return {"context_id": eligibility.context_id, "eligible": eligibility.eligible, "candidate_run_id": eligibility.candidate_run_id, "candidate_status": eligibility.candidate_status, "predecessor_loop_id": eligibility.predecessor_loop_id, "reason": eligibility.reason, "consistency_token": eligibility.consistency_token}

    @staticmethod
    def _validate_wait_answer(request: LoopWaitRequest, answer: dict) -> None:
        if request.response_mode == "text":
            text = answer.get("text")
            if not isinstance(text, str) or not text.strip():
                raise HTTPException(422, "等待请求需要非空 text")
        if request.response_mode == "action":
            action = answer.get("action")
            allowed = {item.get("action") for item in request.response_contract.get("actions", [])}
            if action not in allowed:
                raise HTTPException(422, {"code": "invalid_wait_action", "allowed": sorted(item for item in allowed if item)})
        if request.response_mode == "single_choice":
            selected = answer.get("choice")
            allowed = {str(item.get("value")) for item in request.response_contract.get("options", [])}
            if str(selected) not in allowed:
                raise HTTPException(422, {"code": "invalid_wait_choice", "allowed": sorted(allowed)})
        if request.response_mode == "multiple_choice":
            selected = answer.get("choices")
            allowed = {str(item.get("value")) for item in request.response_contract.get("options", [])}
            if not isinstance(selected, list) or not selected or any(str(item) not in allowed for item in selected):
                raise HTTPException(422, {"code": "invalid_wait_choices", "allowed": sorted(allowed)})
        if request.response_mode == "structured":
            fields = request.response_contract.get("fields", {})
            missing = [name for name, definition in fields.items() if definition.get("required") and answer.get(name) in {None, ""}]
            if missing:
                raise HTTPException(422, {"code": "missing_wait_fields", "fields": missing})

    @staticmethod
    async def _apply_wait_budget_revision(session: AsyncSession, loop: AgentLoop, answer: dict) -> None:
        budgets = answer.get("budgets")
        if not isinstance(budgets, dict) or not budgets:
            raise HTTPException(422, "revise_budget 需要 budgets")
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active").with_for_update())
        if grant is None:
            raise HTTPException(409, "Loop 没有 active grant")
        grant.budgets = {**(grant.budgets or {}), **budgets}

    async def _resume_after_wait(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        request: LoopWaitRequest,
        response_id: str,
    ) -> None:
        current = await session.get(LoopRound, loop.current_round_id, with_for_update=True) if loop.current_round_id else None
        if current is not None and current.status not in {"settled", "superseded", "error"}:
            loop.health = "observing"
            return
        resumed = await create_observation_round(
            session,
            loop,
            current,
            self._hash({"initial_context_id": loop.initial_context_id}),
        )
        session.add(resumed)
        loop.current_round_id = resumed.round_id
        loop.health = "observing"
        await self._journal.append(
            session,
            loop.loop_id,
            CanonicalEventDraft(
                kind="loop.round.observed",
                entity_type="round",
                entity_id=resumed.round_id,
                entity_revision=max(1, resumed.number),
                correlation_id=request.correlation_id,
                causation_id=response_id,
                payload={"round_id": resumed.round_id, "number": resumed.number, "status": resumed.status, "resumed_from_wait_request_id": request.request_id},
                idempotency_key=f"loop-wait:{request.request_id}:successor-round",
            ),
        )

    async def _append_wait_transition_event(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        request: LoopWaitRequest,
        response_id: str,
        *,
        terminated: bool,
    ) -> None:
        await self._journal.append(
            session,
            loop.loop_id,
            CanonicalEventDraft(
                kind="loop.wait.terminated" if terminated else "loop.wait.resumed",
                entity_type="loop",
                entity_id=loop.loop_id,
                entity_revision=loop.revision,
                correlation_id=request.correlation_id,
                causation_id=response_id,
                payload={"status": loop.status, "health": loop.health, "wait_request_id": request.request_id, "current_round_id": loop.current_round_id},
                idempotency_key=f"loop-wait:{request.request_id}:{'terminated' if terminated else 'resumed'}",
            ),
        )

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

    @staticmethod
    def _activation_lock_key(run_id: str) -> int:
        return int.from_bytes(hashlib.sha256(f"loop-activation:{run_id}".encode("utf-8")).digest()[:8], "big", signed=True)
