r"""本文件对外提供 LoopAuthorityService，承接用户对 Patrol delegation 的根权力变更。

输入为 Loop id 与 narrow、adjust_budgets 或 revoke 的封闭请求（含可撤销压缩 policy）；输出为新的 authority revision 与
活动 Run 处理结果。具体工作流为锁定 Loop/grant，撤销旧 grant，使旧 Patrol 工作失效，安全中断旧
authority 下的 Run；narrow/adjust 创建新 grant 和观察轮，revoke 则进入 waiting_user。
示例：`await service.mutate(loop_id, request)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.budgets import LoopBudgetGuard, configured_provider_count
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopContextMembership,
    LoopDecision,
    LoopDelegationGrant,
    LoopDirective,
    LoopEventOutbox,
    LoopRound,
)
from backend.app.desktop.agent_loop.schemas import AdjustLoopBudgetsRequest, LoopGrantMutationRequest, NarrowLoopGrantRequest
from backend.app.desktop.agent_loop.rounds import create_observation_round
from backend.app.desktop.agent_loop.compression_authority.contracts import AutonomousCompressionPolicy
from backend.app.desktop.agent_loop.compression_authority.repository import CompressionAuthorityRepository
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.workspace_coordination.models import WorkspaceLease


class LoopAuthorityService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], run_manager=None) -> None:
        self._sessions = sessions
        self._run_manager = run_manager
        self._directives = DirectiveLifecycleRepository()

    async def mutate(self, loop_id: str, request: LoopGrantMutationRequest) -> dict:
        async with self._sessions.begin() as session:
            loop = await session.scalar(select(AgentLoop).where(AgentLoop.loop_id == loop_id).with_for_update())
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            if loop.status in {"completed", "stopped", "failed"}:
                raise HTTPException(409, f"Loop 状态 {loop.status} 不允许修改授权")
            grant = await session.scalar(
                select(LoopDelegationGrant)
                .where(LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active")
                .with_for_update()
            )
            if grant is None:
                raise HTTPException(409, "Agent Loop 当前没有可修改的活动授权")
            prior_round = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
            replacement = self._replacement(loop, grant, request)
            loop.revision += 1
            loop.authority_revision += 1
            grant.status = "revoked"
            grant.revoked_at = datetime.now(UTC)
            await self._supersede_uncommitted(session, loop_id)
            await CompressionAuthorityRepository().supersede(session, loop_id, "authority_changed")
            interrupted = await self._interrupt_active_runs(session, loop_id)
            if replacement is None:
                loop.status = "waiting_user"
                loop.health = "idle"
                loop.waiting_reason = "用户已撤销 Patrol delegation"
                event_type = "LoopGrantRevoked"
            else:
                replacement.revision = loop.authority_revision
                session.add(replacement)
                expired = replacement.expires_at is not None and self._as_utc(replacement.expires_at) <= datetime.now(UTC)
                exhausted = ("delegation_expired",) if expired else await self._exhausted_budgets(session, loop, replacement.budgets)
                if exhausted:
                    loop.status = "waiting_user"
                    loop.health = "degraded"
                    loop.waiting_reason = "更新后的 Loop hard budget 已耗尽: " + ", ".join(exhausted)
                else:
                    round_row = await create_observation_round(session, loop, prior_round, "0" * 64)
                    loop.current_round_id = round_row.round_id
                    loop.status = "running"
                    loop.health = "observing"
                    loop.waiting_reason = None
                    session.add(round_row)
                event_type = "LoopGrantNarrowed" if isinstance(request, NarrowLoopGrantRequest) else "LoopBudgetsAdjusted"
            await self._append_event(
                session,
                loop,
                event_type,
                {"authority_revision": loop.authority_revision, "interrupted_run_ids": interrupted},
            )
            return {"loop_id": loop.loop_id, "authority_revision": loop.authority_revision, "interrupted_run_ids": interrupted}

    @staticmethod
    def _replacement(loop: AgentLoop, grant: LoopDelegationGrant, request: LoopGrantMutationRequest) -> LoopDelegationGrant | None:
        if request.command == "revoke":
            return None
        capabilities = list(grant.capabilities)
        context_scope = list(grant.context_scope)
        permission_scope = list(grant.permission_scope)
        delegable_gates = list(grant.delegable_gates)
        compression_policy = dict(grant.compression_policy or {})
        budgets = dict(grant.budgets)
        expires_at = grant.expires_at
        if isinstance(request, NarrowLoopGrantRequest):
            LoopAuthorityService._require_subset("capabilities", request.capabilities, grant.capabilities)
            LoopAuthorityService._require_subset("context_scope", request.context_scope, grant.context_scope)
            LoopAuthorityService._require_subset("permission_scope", request.permission_scope, grant.permission_scope)
            LoopAuthorityService._require_subset("delegable_gates", request.delegable_gates, grant.delegable_gates)
            capabilities = list(request.capabilities)
            context_scope = list(request.context_scope)
            permission_scope = list(request.permission_scope)
            delegable_gates = list(request.delegable_gates)
            if "compression" not in delegable_gates:
                compression_policy = {}
            elif request.compression_policy is not None:
                LoopAuthorityService._require_narrower_compression(
                    request.compression_policy,
                    AutonomousCompressionPolicy.model_validate(grant.compression_policy),
                )
                compression_policy = request.compression_policy.model_dump(mode="json")
            if request.expires_at is not None:
                candidate = LoopAuthorityService._as_utc(datetime.fromisoformat(request.expires_at.replace("Z", "+00:00")))
                current_expiry = LoopAuthorityService._as_utc(grant.expires_at) if grant.expires_at else None
                if current_expiry is not None and candidate > current_expiry:
                    raise HTTPException(422, "narrow 不得延长 delegation 到期时间")
                expires_at = candidate
        elif isinstance(request, AdjustLoopBudgetsRequest):
            budgets = request.budgets.model_dump()
        return LoopDelegationGrant(
            grant_id=uuid.uuid4().hex,
            loop_id=loop.loop_id,
            revision=loop.authority_revision + 1,
            holder_id=grant.holder_id,
            capabilities=capabilities,
            context_scope=context_scope,
            permission_scope=permission_scope,
            budgets=budgets,
            delegable_gates=delegable_gates,
            compression_policy=compression_policy,
            expires_at=expires_at,
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _require_subset(name: str, requested, current) -> None:
        extra = set(requested) - set(current)
        if extra:
            raise HTTPException(422, {"code": "grant_not_narrower", "field": name, "extra": sorted(extra)})

    @staticmethod
    def _require_narrower_compression(requested: AutonomousCompressionPolicy, current: AutonomousCompressionPolicy) -> None:
        invalid = (
            (requested.allow_delete and not current.allow_delete)
            or requested.max_source_messages > current.max_source_messages
            or requested.max_source_tokens > current.max_source_tokens
            or requested.max_attempts_per_gate > current.max_attempts_per_gate
            or requested.candidate_ttl_seconds > current.candidate_ttl_seconds
            or requested.min_reduction_tokens < current.min_reduction_tokens
            or not set(requested.protected_anchors).issuperset(current.protected_anchors)
        )
        if invalid:
            raise HTTPException(422, {"code": "compression_policy_not_narrower"})

    async def _interrupt_active_runs(self, session: AsyncSession, loop_id: str) -> list[str]:
        runs = list((await session.scalars(select(DesktopRun).where(DesktopRun.loop_id == loop_id, DesktopRun.status.in_(["pending", "running"])).with_for_update())).all())
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
        return [run.run_id for run in runs]

    async def _supersede_uncommitted(self, session: AsyncSession, loop_id: str) -> None:
        await session.execute(update(LoopDecision).where(LoopDecision.loop_id == loop_id, LoopDecision.status.in_(["pending", "publishing", "adopting"])).values(status="superseded"))
        await session.execute(update(LoopRound).where(LoopRound.loop_id == loop_id, LoopRound.status.in_(["observed", "curated", "ready", "waiting_workers", "publishing", "adopting"])).values(status="superseded"))
        await self._directives.cancel_active(session, loop_id, "authority_changed")

    @staticmethod
    async def _exhausted_budgets(session: AsyncSession, loop: AgentLoop, budgets: dict) -> tuple[str, ...]:
        usage = await session.get(LoopBudgetUsage, loop.loop_id, with_for_update=True)
        context_count = int(
            await session.scalar(
                select(func.count()).select_from(LoopContextMembership).where(
                    LoopContextMembership.loop_id == loop.loop_id,
                    LoopContextMembership.status != "discarded",
                )
            )
            or 0
        )
        created_at = LoopAuthorityService._as_utc(loop.created_at or datetime.now(UTC))
        measured = {
            "rounds": usage.rounds if usage else 0,
            "duration_seconds": max(0, int((datetime.now(UTC) - created_at).total_seconds())),
            "model_calls": usage.model_calls if usage else 0,
            "input_tokens": usage.input_tokens if usage else 0,
            "output_tokens": usage.output_tokens if usage else 0,
            "retries": usage.retries if usage else 0,
            "lanes": usage.lanes if usage else 0,
            "contexts": context_count,
            "providers": configured_provider_count(loop.equipment or {}),
        }
        return LoopBudgetGuard().evaluate(measured, budgets, "grant_update").reasons

    @staticmethod
    async def _append_event(session: AsyncSession, loop: AgentLoop, event_type: str, payload: dict) -> None:
        sequence = int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop.loop_id)) or 0) + 1
        session.add(LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop.loop_id, sequence=sequence, event_type=event_type, payload=payload, idempotency_key=f"{loop.loop_id}:{loop.revision}:{event_type}"))
