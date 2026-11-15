"""本文件对外提供活动 Workspace Patrol 固定无上限预算的启动前迁移。

输入为已迁移数据库的 session factory；输出为本次更新的 Loop identity。
具体工作流为逐个锁定 Loop 和活动授权，以新授权替代旧产品配额并收敛旧执行；旧授权预算、
终态历史和消费账本保留。暂停和人工等待不自动恢复，旧预算等待改为显式重试或停止。
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

_REASON = "fixed_unlimited_budget_upgrade"
_PROMPT = "Loop 执行预算上限已移除。请确认继续执行，或停止本次工作。"
_CONTRACT = {"actions": [{"action": "retry", "label": "继续执行"}, {"action": "stop", "label": "停止 Loop"}]}


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
                LoopWaitRequest.loop_id == loop_id, LoopWaitRequest.kind == "budget_action",
                LoopWaitRequest.status == "open",
            ).with_for_update()))
            pending_waits = [wait for wait in waits if wait.response_contract != _CONTRACT or wait.prompt != _PROMPT]
            budgets = unlimited_loop_budgets()
            if grant.budgets == budgets and not pending_waits:
                continue
            loop.revision += 1
            if grant.budgets != budgets:
                prior = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
                grant.status, grant.revoked_at = "revoked", datetime.now(UTC)
                loop.authority_revision += 1
                await session.flush()
                session.add(LoopDelegationGrant(
                    grant_id=uuid.uuid4().hex, loop_id=loop_id, revision=loop.authority_revision,
                    holder_id=grant.holder_id, capabilities=list(grant.capabilities),
                    context_scope=list(grant.context_scope), permission_scope=list(grant.permission_scope),
                    budgets=budgets, delegable_gates=list(grant.delegable_gates),
                    compression_policy=dict(grant.compression_policy or {}), expires_at=grant.expires_at,
                ))
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
                await LoopEventJournal().append(session, loop_id, CanonicalEventDraft(
                    kind="loop.wait.request_opened", entity_type="loop_wait_request",
                    entity_id=wait.request_id, entity_revision=wait.revision,
                    correlation_id=wait.correlation_id, causation_id=wait.causation_id,
                    payload={key: getattr(wait, key) for key in (
                        "request_id", "kind", "prompt", "response_mode", "response_contract", "scope",
                        "status", "round_id", "created_by",
                    )},
                    idempotency_key=f"unlimited-budget-wait:{wait.request_id}:{wait.revision}",
                ))
            await LoopLifecycleEventRecorder().record(session, loop, cause_id=_REASON)
            upgraded.append(loop_id)
    return tuple(upgraded)
