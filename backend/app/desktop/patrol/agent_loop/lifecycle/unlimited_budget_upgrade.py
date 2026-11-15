"""本文件对外提供活动 Workspace Patrol 固定无上限预算的启动前迁移。

输入为已迁移数据库的 session factory；输出为本次更新的 Loop identity。
具体工作流为逐个锁定 Loop 和活动授权，以新授权替代旧产品配额并收敛旧执行；旧授权预算、
终态历史和消费账本保留。暂停和人工等待不自动恢复，旧预算等待改为显式重试或停止。
旧 Worker 恢复改为显式重新观察；类型化缺失目标等待保留授权迁移谱系，人工审批和信息合同保留。
示例：await upgrade_workspace_loop_budgets(sessions)，仅在正常启动恢复执行前调用。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.patrol.agent_loop.compression_authority.repository import (
    CompressionAuthorityRepository,
)
from backend.app.desktop.patrol.agent_loop.curation.curation_ownership import (
    TERMINAL_LOOP_STATUSES,
)
from backend.app.desktop.patrol.agent_loop.journal.event_contract import (
    CanonicalEventDraft,
)
from backend.app.desktop.patrol.agent_loop.journal.event_journal import LoopEventJournal
from backend.app.desktop.patrol.agent_loop.journal.lifecycle_events import (
    LoopLifecycleEventRecorder,
)
from backend.app.desktop.patrol.agent_loop.lifecycle.runtime_convergence import (
    LoopRuntimeConvergence,
)
from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop,
    LoopDelegationGrant,
    LoopRound,
)
from backend.app.desktop.patrol.agent_loop.resources.unlimited_budget import (
    unlimited_loop_budgets,
)
from backend.app.desktop.patrol.agent_loop.rounds.lifecycle import (
    create_observation_round,
)
from backend.app.desktop.patrol.agent_loop.waits.wait_models import LoopWaitRequest
from backend.app.desktop.patrol.agent_loop.waits.wait_requests import (
    LoopWaitRequestService,
    WaitRequestDraft,
)

_REASON = "fixed_unlimited_budget_upgrade"
_PROMPT = "Loop 执行预算上限已移除。请确认继续执行，或停止本次工作。"
_CONTRACT = {"actions": [{"action": "retry", "label": "继续执行"}, {"action": "stop", "label": "停止 Loop"}]}
_REOBSERVE_PROMPT = "升级已使原执行恢复身份失效。请重新观察并安排工作，或停止本次工作。"
_REOBSERVE_CONTRACT = {"actions": [{"action": "retry", "label": "重新观察并继续"}, {"action": "stop", "label": "停止 Loop"}]}


def _execution_recovery(wait):
    scope = wait.scope or {}
    if (wait.kind != "recovery_action" or scope.get("information_only")
            or scope.get("kind") == "access_approval" or scope.get("pending_decision_id")
            or scope.get("cause") in {"permission", "missing_goal", "missing_input"}
            or (scope.get("evidence_identity") or {}).get("kind") == "mission"):
        return False
    return bool(scope.get("worker_request_id"))


async def _mission_recovery_sources(session, loop, waits, grant, replacing):
    sources = []
    if loop.status != "waiting_user":
        return sources
    current = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
    for wait in waits:
        scope = wait.scope or {}
        evidence = scope.get("evidence_identity") or {}
        if (wait.kind != "clarification" or wait.response_mode != "text" or scope.get("cause") != "missing_goal"
                or scope.get("information_only") or scope.get("authority_upgrade")
                or evidence.get("kind") != "mission" or evidence.get("reference_id") != "outcome"
                or current is None or current.status not in {"settled", "error"}
                or wait.round_id not in {None, current.round_id} or current.goal_revision != loop.goal_revision
                or scope.get("mission_revision") not in {None, loop.goal_revision}
                or evidence.get("revision") not in {None, loop.goal_revision}):
            continue
        source_grant = grant if current.authority_revision == grant.revision else await session.scalar(
            select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id,
                                             LoopDelegationGrant.revision == current.authority_revision))
        if source_grant is None:
            continue
        if (replacing and source_grant.grant_id == grant.grant_id) or (not replacing and source_grant.status == "revoked"
              and source_grant.revision + 1 == grant.revision
              and source_grant.budgets != unlimited_loop_budgets()
              and all(getattr(source_grant, key) == getattr(grant, key) for key in (
                  "holder_id", "capabilities", "context_scope", "permission_scope", "delegable_gates",
                  "compression_policy", "expires_at"))):
            sources.append((wait, current, source_grant))
    return sources


async def _record_wait_revision(session, loop_id, wait, key):
    await LoopEventJournal().append(session, loop_id, CanonicalEventDraft(
        kind="loop.wait.request_opened", entity_type="loop_wait_request",
        entity_id=wait.request_id, entity_revision=wait.revision,
        correlation_id=wait.correlation_id, causation_id=wait.causation_id,
        payload={name: getattr(wait, name) for name in (
            "request_id", "kind", "prompt", "response_mode", "response_contract", "scope",
            "status", "round_id", "created_by",
        )}, idempotency_key=key))


async def upgrade_workspace_loop_budgets(sessions: async_sessionmaker[AsyncSession]) -> tuple[str, ...]:
    """Replace finite active authority without resuming a paused or waiting Loop."""
    async with sessions() as session:
        identities = tuple(await session.scalars(select(AgentLoop.loop_id).where(
            AgentLoop.interaction_mode == "workspace_patrol",
            AgentLoop.status.not_in(TERMINAL_LOOP_STATUSES),
        ).order_by(AgentLoop.loop_id)))
    upgraded = []
    for loop_id in identities:
        async with sessions.begin() as session:
            loop = await session.get(AgentLoop, loop_id, with_for_update=True)
            if loop is None or loop.interaction_mode != "workspace_patrol" or loop.status in TERMINAL_LOOP_STATUSES:
                continue
            grant = await session.scalar(select(LoopDelegationGrant).where(
                LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active",
                LoopDelegationGrant.revision == loop.authority_revision,
            ).with_for_update())
            if grant is None:
                continue
            waits = tuple(await session.scalars(select(LoopWaitRequest).where(
                LoopWaitRequest.loop_id == loop_id,
                LoopWaitRequest.status == "open",
            ).with_for_update()))
            pending_waits = [wait for wait in waits if wait.kind == "budget_action"
                             and (wait.response_contract != _CONTRACT or wait.prompt != _PROMPT)]
            budgets = unlimited_loop_budgets()
            mission_waits = await _mission_recovery_sources(session, loop, waits, grant, grant.budgets != budgets)
            execution_waits = []
            for wait in waits:
                if not _execution_recovery(wait):
                    continue
                source = await session.get(LoopRound, wait.round_id) if wait.round_id else None
                if grant.budgets != budgets or (source is not None and (
                    source.authority_revision != loop.authority_revision or source.status == "superseded"
                )):
                    execution_waits.append(wait)
            if grant.budgets == budgets and not pending_waits and not execution_waits and not mission_waits:
                continue
            loop.revision += 1
            active_grant = grant
            if grant.budgets != budgets:
                prior = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
                grant.status, grant.revoked_at = "revoked", datetime.now(UTC)
                loop.authority_revision += 1
                await session.flush()
                active_grant = LoopDelegationGrant(
                    grant_id=uuid.uuid4().hex, loop_id=loop_id, revision=loop.authority_revision,
                    holder_id=grant.holder_id, capabilities=list(grant.capabilities),
                    context_scope=list(grant.context_scope), permission_scope=list(grant.permission_scope),
                    budgets=budgets, delegable_gates=list(grant.delegable_gates),
                    compression_policy=dict(grant.compression_policy or {}), expires_at=grant.expires_at,
                )
                session.add(active_grant)
                await LoopRuntimeConvergence().converge(session, loop, _REASON, preserve_waits=True)
                await CompressionAuthorityRepository().supersede(session, loop_id, _REASON)
                if loop.status == "running":
                    successor = await create_observation_round(session, loop, prior, "0" * 64)
                    session.add(successor)
                    loop.current_round_id, loop.health = successor.round_id, "observing"
            for wait in pending_waits:
                wait.prompt, wait.response_contract = _PROMPT, _CONTRACT
                wait.revision += 1
                if loop.status == "waiting_user" and wait.round_id in {None, loop.current_round_id}:
                    loop.waiting_reason = _PROMPT
                await _record_wait_revision(session, loop_id, wait, f"unlimited-budget-wait:{wait.request_id}:{wait.revision}")
            for wait, source_round, source_grant in mission_waits:
                marker = {"kind": _REASON, "source_round_id": source_round.round_id,
                          "source_authority_revision": source_round.authority_revision,
                          "source_grant_id": source_grant.grant_id, "grant_id": active_grant.grant_id,
                          "authority_revision": loop.authority_revision, "mission_revision": loop.goal_revision}
                wait.scope = {**wait.scope, "authority_upgrade": marker}
                wait.revision += 1
                await _record_wait_revision(session, loop_id, wait,
                                            f"budget-upgrade-mission-wait:{wait.request_id}:{wait.revision}")
            for wait in execution_waits:
                waits_service = LoopWaitRequestService()
                await waits_service.cancel(session, wait.request_id, superseded=True)
                await waits_service.open(session, loop, WaitRequestDraft(
                    kind="recovery_action", prompt=_REOBSERVE_PROMPT, response_mode="action",
                    response_contract=_REOBSERVE_CONTRACT,
                    scope={"component": "authority_upgrade", "superseded_request_id": wait.request_id},
                ), created_by="unlimited-budget-upgrade",
                    correlation_id=f"unlimited-budget-recovery:{wait.request_id}",
                    causation_id=wait.request_id)
            await LoopLifecycleEventRecorder().record(session, loop, cause_id=_REASON)
            upgraded.append(loop_id)
    return tuple(upgraded)
