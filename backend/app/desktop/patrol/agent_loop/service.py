r"""本文件对外提供 Workspace Patrol 的查询、类型化等待响应、沿用当前 Mission 的显式恢复、控制与事件读取用例。

输入为认证后的 Loop id、控制动作或等待响应；输出为包含有效 Expansion 策略、用量和阻断的 Loop 快照与持久事件。
具体工作流为从 WorkspacePatrolService 的首次输入创建或终态后继读取状态，目标与边界通过工作区统一输入修订。
pause/resume 推进 revision，stop 委托 TerminalLifecycle 原子收敛运行时并释放 Lane；失败轮恢复时新建观察轮并累计 retry。
记忆与 Worker 恢复、等待响应按 Loop → 等待请求锁定，原子提交并保留冻结输入及单调尝试身份。
控制事务提交后才通知执行器取消，Run 租约由真实结算释放。
已提交等待响应的幂等重放只返回历史提交身份和当前状态，不重复执行停止或恢复效果；预算固定无上限，不接受等待响应修改。
材料修订响应在已锁定 Loop 的等待事务内复查实际授权、目标和耐久次数，普通 retry 保持原恢复含义。
人工访问门禁的正式 retry 先核对同主体人工恢复及严格检查点，不代替用户批准工具。
workspace idle 可显式暂停，信息型卡片不作为全局恢复 gate。示例：`await service.get(loop_id)`。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.patrol.agent_loop.resources.budgets import configured_provider_count
from backend.app.desktop.patrol.agent_loop.compression_authority.repository import (
    CompressionAuthorityRepository,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.models import (
    LoopPlanningRetrievalSession,
)
from backend.app.desktop.patrol.agent_loop.curation.curation_ownership import (
    CurationOwnershipRepository,
)
from backend.app.desktop.patrol.agent_loop.journal.event_contract import CanonicalEventDraft
from backend.app.desktop.patrol.agent_loop.journal.event_journal import LoopEventJournal
from backend.app.desktop.patrol.agent_loop.resources.expansion_resource_projection import (
    expansion_resource_view,
    planning_session_view,
)
from backend.app.desktop.patrol.agent_loop.missions.mission_models import LoopMissionRevision
from backend.app.desktop.patrol.agent_loop.recovery.mission_wait_recovery import (
    MissionWaitRecoveryGuard,
    MissionWaitRecoveryRejected,
)
from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopContextMembership,
    LoopDelegationGrant,
    LoopEventOutbox,
    LoopGoalRevision,
    LoopRound,
)
from backend.app.desktop.patrol.agent_loop.rounds.lifecycle import create_observation_round
from backend.app.desktop.patrol.agent_loop.lifecycle.runtime_convergence import LoopRuntimeConvergence
from backend.app.desktop.patrol.agent_loop.schemas import (
    LoopWaitResponseRequest,
    ResumeWithCurrentMissionRequest,
)
from backend.app.desktop.patrol.agent_loop.lifecycle.terminal_lifecycle import LoopTerminalLifecycle
from backend.app.desktop.patrol.agent_loop.resources.usage import LoopUsageDelta, LoopUsageLedger
from backend.app.desktop.patrol.agent_loop.waits.wait_models import LoopWaitRequest
from backend.app.desktop.patrol.agent_loop.waits.wait_requests import (
    LoopWaitRequestService,
    WaitRequestConflict,
)
from backend.app.desktop.patrol.agent_loop.journal.accounting_query import LoopAccountingQuery


class AgentLoopService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], run_manager=None, *, user_gate_recovery=None) -> None:
        self._sessions = sessions
        self._run_manager = run_manager
        self._user_gate_recovery = user_gate_recovery
        self._convergence = LoopRuntimeConvergence()
        self._ownership = CurationOwnershipRepository()
        self._terminal = LoopTerminalLifecycle(self._convergence, self._ownership)
        self._journal = LoopEventJournal()
        self._waits = LoopWaitRequestService()
        self._mission_wait_recovery = MissionWaitRecoveryGuard()

    async def get(self, loop_id: str) -> dict:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, loop_id)
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            return await self._snapshot(session, loop)

    async def ensure_context_main_input_allowed(self, context_id: str) -> None:
        from backend.app.desktop.patrol.agent_loop.inputs.context_main_guard import ensure_context_main_input_allowed

        async with self._sessions() as session:
            await ensure_context_main_input_allowed(session, context_id)

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
            loop = await session.get(AgentLoop, loop_id, with_for_update=True)
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            self._require_workspace_patrol(loop)
            request = await session.get(LoopWaitRequest, request_id, with_for_update=True)
            if request is None or request.loop_id != loop_id:
                raise HTTPException(404, "等待请求不存在")
            self._validate_wait_answer(request, body.answer)
            if (request.kind == "budget_action" or request.scope.get("component") == "authority_upgrade") and body.answer.get("action") == "retry" and request.status == "open":
                grant = await session.scalar(select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == loop_id,
                    LoopDelegationGrant.status == "active",
                    LoopDelegationGrant.revision == loop.authority_revision,
                ).with_for_update())
                expires_at = None if grant is None else grant.expires_at
                if expires_at is not None and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if grant is None or (expires_at is not None and expires_at <= datetime.now(UTC)):
                    raise HTTPException(409, "Patrol delegation 已撤销或过期，不能恢复执行")
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
            action = str(body.answer.get("action") or "")
            if created and action == "revise_material":
                from backend.app.desktop.patrol.agent_loop.context_expansion.quality_revision import admit_manual_revision

                try:
                    await admit_manual_revision(session, loop, request.scope.get("quality_recovery"))
                except ValueError as exc:
                    raise HTTPException(409, {"code": "quality_revision_not_admitted", "message": str(exc)}) from exc
            if created and action == "retry" and request.scope.get("kind") == "access_approval":
                if self._user_gate_recovery is None:
                    raise HTTPException(409, {"code": "user_gate_not_resolved", "message": "人工访问审批缺少来源核对端口"})
                try:
                    await self._user_gate_recovery.resolve(session, loop, request, response.response_id, actor_id)
                except ValueError as exc:
                    raise HTTPException(409, {"code": "user_gate_not_resolved", "message": str(exc)}) from exc
            if created and action == "stop":
                loop.revision += 1
                await self._terminal.finalize(session, loop, "stopped", "wait_request_stop")
            elif created and action == "retry" and request.scope.get("component") == "progress_memory":
                from backend.app.desktop.patrol.agent_loop.task_progress.repository import TaskProgressRepository

                try:
                    await TaskProgressRepository().retry(session, request.scope.get("work_id") or request.scope["observation_id"], loop_id=loop_id)
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
            elif created and action == "retry" and request.scope.get("worker_request_id"):
                from backend.app.desktop.patrol.agent_loop.recovery.worker_recovery import WorkerRecoveryRepository

                try:
                    await WorkerRecoveryRepository().retry(session, loop, request)
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
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

    async def resume_with_current_mission(
        self,
        loop_id: str,
        request_id: str,
        body: ResumeWithCurrentMissionRequest,
        *,
        actor_id: str,
    ) -> dict:
        async with self._sessions.begin() as session:
            loop = await session.get(AgentLoop, loop_id, with_for_update=True)
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            self._require_workspace_patrol(loop)
            request = await session.get(LoopWaitRequest, request_id, with_for_update=True)
            if request is None or request.loop_id != loop_id:
                raise HTTPException(404, "等待请求不存在")
            event_key = "mission-wait-recovery:" + hashlib.sha256(body.idempotency_key.encode("utf-8")).hexdigest()
            existing = await session.scalar(select(LoopEventOutbox).where(LoopEventOutbox.idempotency_key == event_key))
            if existing is not None:
                payload = existing.payload or {}
                if payload.get("request_id") != request_id or payload.get("request_revision") != body.request_revision or payload.get("actor_id") != actor_id:
                    raise HTTPException(409, "恢复幂等键已用于不同请求")
                return {"request": self._wait_request_payload(request), "recovery_event_id": existing.event_id, "created": False, "loop_status": loop.status, "current_round_id": payload["successor_round_id"]}
            try:
                await self._mission_wait_recovery.validate(session, loop, request, request_revision=body.request_revision)
            except MissionWaitRecoveryRejected as exc:
                raise HTTPException(409, str(exc)) from exc
            await self._waits.cancel(session, request_id, superseded=True)
            loop.status = "running"
            loop.waiting_reason = None
            loop.revision += 1
            recovery_event_id = uuid.uuid4().hex
            await self._resume_after_wait(session, loop, request, recovery_event_id)
            await self._append_wait_transition_event(session, loop, request, recovery_event_id, terminated=False)
            sequence = int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop_id)) or 0) + 1
            session.add(LoopEventOutbox(
                event_id=recovery_event_id,
                loop_id=loop_id,
                sequence=sequence,
                event_type="MissionWaitRecoveryConfirmed",
                payload={"request_id": request_id, "request_revision": body.request_revision, "actor_id": actor_id, "mission_revision": loop.goal_revision, "successor_round_id": loop.current_round_id},
                idempotency_key=event_key,
            ))
            return {"request": self._wait_request_payload(request), "recovery_event_id": recovery_event_id, "created": True, "loop_status": loop.status, "current_round_id": loop.current_round_id}

    async def control(self, loop_id: str, command: str) -> dict:
        transitions = {"pause": ({"running"}, "paused"), "resume": ({"paused", "waiting_user"}, "running"), "stop": ({"running", "paused", "waiting_user"}, "stopped")}
        if command not in transitions:
            raise HTTPException(422, "未知 Loop 控制命令")
        async with self._sessions.begin() as session:
            loop = await session.scalar(select(AgentLoop).where(AgentLoop.loop_id == loop_id).with_for_update())
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            self._require_workspace_patrol(loop)
            allowed, target = transitions[command]
            if command == "pause" and loop.interaction_mode == "workspace_patrol" and loop.waiting_reason == "awaiting_input":
                allowed = allowed | {"waiting_user"}
            if loop.status not in allowed:
                raise HTTPException(409, f"Loop 状态 {loop.status} 不允许 {command}")
            if command == "resume" and loop.status == "waiting_user":
                active_wait = await self._waits.active(session, loop_id, lock=True)
                if active_wait is not None and not active_wait.scope.get("information_only"):
                    raise HTTPException(409, {"code": "wait_response_required", "request_id": active_wait.request_id})
            if command == "resume":
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active"))
                if grant is None:
                    raise HTTPException(409, "Patrol delegation 已撤销，不能恢复无人值守 Loop")
                current = await session.get(LoopRound, loop.current_round_id, with_for_update=True) if loop.current_round_id else None
                pending = await session.scalar(select(LoopRound).where(LoopRound.loop_id == loop.loop_id,
                    LoopRound.decision_id.is_(None), LoopRound.status.in_(("observed", "curated", "waiting_workers"))).with_for_update())
                if pending is not None:
                    current = pending
                    loop.current_round_id = current.round_id
                    current.status = "observed"
                    current.barrier = {**(current.barrier or {}), "refresh_observation": bool(current.observation_id)}
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
            run_ids: tuple[str, ...] = ()
            loop.revision += 1
            if command == "pause":
                loop.status = target
                loop.health = "idle"
                await CompressionAuthorityRepository().supersede(session, loop_id, "loop_paused")
                run_ids = await self._convergence.converge(session, loop, "loop_paused")
            elif command == "stop":
                finalized = await self._terminal.finalize(session, loop, "stopped", "loop_stopped")
                run_ids = finalized.cancelled_run_ids
            else:
                loop.status = target
                loop.health = "observing"
            await self._append_event(session, loop, f"Loop{command.title()}", {"status": target})
            snapshot = await self._snapshot(session, loop)
        self._notify_run_cancellation(run_ids)
        return snapshot

    async def events(self, loop_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        async with self._sessions() as session:
            rows = list((await session.scalars(select(LoopEventOutbox).where(LoopEventOutbox.loop_id == loop_id, LoopEventOutbox.sequence > after).order_by(LoopEventOutbox.sequence).limit(limit))).all())
            return [{"event_id": row.event_id, "cursor": row.sequence, "type": row.event_type, "payload": row.payload, "created_at": row.created_at.isoformat()} for row in rows]

    def _notify_run_cancellation(self, run_ids: tuple[str, ...]) -> None:
        if self._run_manager is not None:
            for run_id in run_ids:
                self._run_manager.cancel(run_id, action="interrupt")

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
        planning_rows = list((await session.execute(
            select(LoopPlanningRetrievalSession, LoopRound.number)
            .join(LoopRound, LoopRound.round_id == LoopPlanningRetrievalSession.round_id)
            .where(LoopPlanningRetrievalSession.loop_id == loop.loop_id)
            .order_by(LoopRound.number.desc(), LoopPlanningRetrievalSession.updated_at.desc(), LoopPlanningRetrievalSession.session_id.desc())
            .limit(24)
        )).all())
        expansion_resources = None if grant is None else expansion_resource_view(
            grant.budgets,
            grant.revision,
            {} if usage is None else {"model_calls": usage.model_calls, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
            tuple(planning_session_view(row.payload, round_id=row.round_id, round_number=number) for row, number in reversed(planning_rows)),
        )
        accounting = await LoopAccountingQuery().read(session, loop.loop_id)
        return {"loop_id": loop.loop_id, "workspace_id": loop.workspace_id, "interaction_mode": loop.interaction_mode, "initial_context_id": loop.initial_context_id, "program_id": loop.program_id, "status": loop.status, "health": loop.health, "revision": loop.revision, "goal_revision": loop.goal_revision, "active_mission_revision": loop.goal_revision, "authority_revision": loop.authority_revision, "holder_id": loop.holder_id, "current_round_id": loop.current_round_id, "current_portfolio_revision_id": loop.current_portfolio_revision_id, "waiting_reason": loop.waiting_reason, "wait_request": None if wait_request is None else self._wait_request_payload(wait_request), "equipment": loop.equipment, "accounting": accounting, "mission": mission_payload, "goal": None if goal is None else {"goal": goal.goal, "task_contract": goal.task_contract, "acceptance_criteria": goal.acceptance_criteria}, "grant": None if grant is None else {"grant_id": grant.grant_id, "status": grant.status, "capabilities": grant.capabilities, "context_scope": grant.context_scope, "permission_scope": grant.permission_scope, "budgets": grant.budgets, "delegable_gates": grant.delegable_gates, "compression_policy": grant.compression_policy, "expires_at": grant.expires_at.isoformat() if grant.expires_at else None}, "usage": None if usage is None else {"rounds": accounting["completed_rounds"], "duration_seconds": duration_seconds, "model_calls": usage.model_calls, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens, "retries": usage.retries, "lanes": usage.lanes, "contexts": context_count, "providers": configured_provider_count(loop.equipment or {}), "no_progress_count": usage.no_progress_count}, "expansion_resources": expansion_resources, "final_result": loop.final_result}

    @staticmethod
    def _wait_request_payload(request: LoopWaitRequest) -> dict:
        return {"request_id": request.request_id, "loop_id": request.loop_id, "round_id": request.round_id, "kind": request.kind, "prompt": request.prompt, "response_mode": request.response_mode, "response_contract": request.response_contract, "scope": request.scope, "status": request.status, "revision": request.revision, "correlation_id": request.correlation_id, "causation_id": request.causation_id, "created_by": request.created_by, "created_at": request.created_at.isoformat() if request.created_at else None}


    @staticmethod
    def _validate_wait_answer(request: LoopWaitRequest, answer: dict) -> None:
        if answer.get("action") == "revise_budget" or "budgets" in answer:
            raise HTTPException(422, {"code": "fixed_unlimited_budget", "message": "Loop 预算固定无上限，不可调整"})
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

    async def _resume_after_wait(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        request: LoopWaitRequest,
        response_id: str,
    ) -> None:
        loop.revision += 1
        current = await session.get(LoopRound, loop.current_round_id, with_for_update=True) if loop.current_round_id else None
        pending = await session.scalar(select(LoopRound).where(LoopRound.loop_id == loop.loop_id,
            LoopRound.decision_id.is_(None), LoopRound.status.in_(("observed", "curated", "waiting_workers"))).with_for_update())
        if pending is not None:
            current = pending
            loop.current_round_id = current.round_id
            if not request.scope.get("worker_request_id") and request.scope.get("component") != "progress_memory":
                current.status = "observed"
                current.barrier = {**(current.barrier or {}), "refresh_observation": bool(current.observation_id)}
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
        from backend.app.desktop.patrol.agent_loop.journal.lifecycle_events import LoopLifecycleEventRecorder
        from backend.app.desktop.patrol.agent_loop.journal.round_events import RoundStateEventRecorder

        await RoundStateEventRecorder().record(session, resumed)
        await LoopLifecycleEventRecorder().record(session, loop, cause_id=response_id)

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
                entity_type="loop_activity",
                entity_id=loop.loop_id,
                entity_revision=loop.revision,
                correlation_id=request.correlation_id,
                causation_id=response_id,
                payload={"status": loop.status, "health": loop.health, "wait_request_id": request.request_id, "current_round_id": loop.current_round_id},
                idempotency_key=f"loop-wait:{request.request_id}:{'terminated' if terminated else 'resumed'}",
            ),
        )

    @staticmethod
    async def _append_event(session, loop, event_type, payload) -> None:
        from backend.app.desktop.patrol.agent_loop.journal.lifecycle_events import LoopLifecycleEventRecorder

        await LoopLifecycleEventRecorder().record(session, loop)
        sequence = int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop.loop_id)) or 0) + 1
        session.add(LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop.loop_id, sequence=sequence, event_type=event_type, payload=payload, idempotency_key=f"{loop.loop_id}:{loop.revision}:{event_type}"))

    @staticmethod
    def _require_workspace_patrol(loop: AgentLoop) -> None:
        if loop.interaction_mode != "workspace_patrol":
            raise HTTPException(409, {"code": "context_loop_retired", "message": "旧 Context Loop 已退役，请通过工作区 Patrol 提交输入"})

    @staticmethod
    def _hash(value) -> str:
        return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()
