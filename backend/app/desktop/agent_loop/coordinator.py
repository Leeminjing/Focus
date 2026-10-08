r"""本文件对外提供 LoopCoordinator、CoordinatorClaim 与基于 per-Loop 独立组件监督的 LoopCoordinatorRuntime。

输入为数据库中的 running Loop、可领取 round、租约事实、Worker/Run/outbox 事实、稳定 compression gate 和
coordinator identity；输出为带 lease 的唯一 round claim、停滞 round 的收敛结果与恢复计数。具体工作流为
skip-locked 领取**未被有效租约持有**的候选 round（过期租约即时清除）、fencing stale attempt、由数据库
状态推进 health；候选快照后新提交的有效租约在锁内复检，已占用候选从本次领取集合排除并顺延，不覆盖他人租约。
运行期在领取前先收敛已无进展的 round 并释放其名额，Runtime 启动时执行完整
AgentLoopRecovery，随后由 registry 为每个 running Loop 建立独立 Supervisor，分别消费 Run、Worker、publication、Context Run、Fact 与 round。
任务记忆拥有 admission 层独立组件，Loop 停止后仍可收口已冻结工作，不恢复执行监督。
Run 结算只推进与该 Directive 当前绑定尝试一致的 Directive：结算的 Run 不是当前绑定时（已被取代的尝试），
只记录该 Run 自身的事实，不改写 Directive 生命周期与绑定。
正常 Round 收口读取全部耐久后果；失败交回用户后不覆盖等待健康状态，既有 maintenance 会重查等待清理的事务并复用相同推进入口；Run 模型消费有逐 attempt receipt 时不重复整 Run 计费，暂停后仍完成消费对账。
稳定推进委托 rounds 唯一事务，进展依据全轮实际语义成果，与最后一个 Run 是否成功无关。
Run 结算先 flush 当前已发生事实，再按 Loop→Round 刷新锁定所有者，之后收口消息与 Directive；该顺序与 Worker、维护和控制事务一致。
执行阶段变化发布独立实体版本，不通过改变控制版本使正在执行的 Run 失效。示例：`runtime = LoopCoordinatorRuntime(...)`。
新直接消息沿 Directive 统一结算，observed 阶段启动失败保留 delivery_failed；有 Directive 的 Run 不重复发布旧用户 Run 事件。
派发接受当前 ready/running Round 中已授权的就绪工作；运行期补位不改变控制版本，活动和排队后果仍经同一收口屏障。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.budgets import (
    LoopBudgetGuard,
    configured_provider_count,
)
from backend.app.desktop.agent_loop.directive_causality import (
    DirectiveCausalityRecorder,
)
from backend.app.desktop.agent_loop.directive_lifecycle import (
    DirectiveLifecycleRepository,
)
from backend.app.desktop.agent_loop.dispatch import LoopWaveDispatcher
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.intervention_lifecycle import (
    InterventionLifecycleRepository,
)
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopAction,
    LoopBudgetUsage,
    LoopContextMembership,
    LoopCoordinatorFence,
    LoopCoordinatorLease,
    LoopDelegationGrant,
    LoopDirective,
    LoopEventOutbox,
    LoopPatrolAttempt,
    LoopRound,
    LoopUserIntent,
    MessageProvenance,
)
from backend.app.desktop.agent_loop.patrol_session_models import LoopPatrolSession
from backend.app.desktop.agent_loop.patrol_session_repository import (
    PatrolSessionRepository,
)
from backend.app.desktop.agent_loop.patrol_session_state import (
    PatrolActivity,
    PatrolPhase,
)
from backend.app.desktop.agent_loop.rounds import (
    CLAIMABLE_ROUND_STATUSES,
    RoundStallLimits,
    current_frontier_hash,
    terminate_round,
    terminate_stalled_rounds,
)
from backend.app.desktop.agent_loop.supervisor import (
    LoopSupervisor,
    SupervisorComponent,
)
from backend.app.desktop.agent_loop.supervisor_registry import LoopSupervisorRegistry
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger
from backend.app.desktop.agent_loop.user_run_events import LoopUserRunEventRecorder
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.models import ModelAttemptAudit
from backend.app.desktop.agent_loop.round_consequences import RoundConsequenceReader
from backend.app.desktop.run_orchestration import RunOutboxConsumer
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


@dataclass(frozen=True, slots=True)
class CoordinatorClaim:
    lease_id: str
    loop_id: str
    round_id: str
    fencing_token: str


class RoundOrchestratorPort(Protocol):
    async def process(self, claim: CoordinatorClaim): ...


class WorkerRuntimePort(Protocol):
    async def drain(self) -> int: ...


class RecoveryPort(Protocol):
    async def reconcile(self): ...


class CompressionGateProjectionPort(Protocol):
    async def project_settled(self, session, event, run, loop): ...


class CompressionResolutionPort(Protocol):
    async def drain(self) -> int: ...
    async def reconcile(self) -> int: ...
    async def settle_run(self, session, event, run, loop) -> str | None: ...


class RoundMaintenancePort(Protocol):
    async def maintain_rounds(self) -> tuple[str, ...]: ...


class SupervisedQueuePort(Protocol):
    async def drain(self) -> int: ...
    async def close(self) -> None: ...


class LoopCoordinator:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        ttl_seconds: int = 60,
        compression_gates: CompressionGateProjectionPort | None = None,
        compression_resolutions: CompressionResolutionPort | None = None,
        stall_limits: RoundStallLimits | None = None,
    ) -> None:
        self._sessions = sessions
        self._ttl = ttl_seconds
        self._compression_gates = compression_gates
        self._compression_resolutions = compression_resolutions
        self._stall_limits = stall_limits or RoundStallLimits()
        self._patrol_sessions = PatrolSessionRepository()
        self._directive_lifecycle = DirectiveLifecycleRepository()
        self._interventions = InterventionLifecycleRepository()
        self._directive_causality = DirectiveCausalityRecorder()
        self._user_run_events = LoopUserRunEventRecorder()

    @property
    def stall_limits(self) -> RoundStallLimits:
        return self._stall_limits

    @property
    def renewal_interval(self) -> float:
        return max(0.1, self._ttl / 3)

    @staticmethod
    def _candidate_statement(now: datetime, loop_id: str | None = None):
        statement = (
            select(LoopRound)
            .join(AgentLoop, AgentLoop.loop_id == LoopRound.loop_id)
            .outerjoin(LoopCoordinatorLease, LoopCoordinatorLease.round_id == LoopRound.round_id)
            .where(
                AgentLoop.status == "running",
                LoopRound.status.in_(CLAIMABLE_ROUND_STATUSES),
                or_(LoopCoordinatorLease.lease_id.is_(None), LoopCoordinatorLease.expires_at <= now),
            )
            .order_by(LoopRound.started_at)
            .with_for_update(of=LoopRound, skip_locked=True)
            .limit(1)
        )
        return statement if loop_id is None else statement.where(LoopRound.loop_id == loop_id)

    async def claim(self, owner_id: str) -> CoordinatorClaim | None:
        return await self._claim(owner_id, None)

    async def claim_for_loop(self, loop_id: str, owner_id: str) -> CoordinatorClaim | None:
        return await self._claim(owner_id, loop_id)

    async def running_loop_ids(self) -> tuple[str, ...]:
        async with self._sessions() as session:
            return tuple((await session.scalars(select(AgentLoop.loop_id).where(AgentLoop.status == "running").order_by(AgentLoop.loop_id))).all())

    async def _claim(self, owner_id: str, loop_id: str | None) -> CoordinatorClaim | None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            await session.execute(delete(LoopCoordinatorLease).where(LoopCoordinatorLease.expires_at <= now))
            round_row, lease = await self._unleased_candidate(session, now, loop_id)
            if round_row is None:
                return None
            fence = await session.get(LoopCoordinatorFence, round_row.round_id, with_for_update=True)
            if fence is None:
                fence = LoopCoordinatorFence(round_id=round_row.round_id, fencing_token=1)
                session.add(fence)
            else:
                fence.fencing_token += 1
            token = str(fence.fencing_token)
            if lease is None:
                lease = LoopCoordinatorLease(lease_id=uuid.uuid4().hex, round_id=round_row.round_id, owner_id=owner_id, fencing_token=token, expires_at=now + timedelta(seconds=self._ttl))
                session.add(lease)
            else:
                lease.owner_id = owner_id
                lease.fencing_token = token
                lease.expires_at = now + timedelta(seconds=self._ttl)
            return CoordinatorClaim(lease.lease_id, round_row.loop_id, round_row.round_id, lease.fencing_token)

    async def _unleased_candidate(self, session, now, loop_id):
        occupied: set[str] = set()
        while True:
            statement = self._candidate_statement(now, loop_id)
            if occupied:
                statement = statement.where(LoopRound.round_id.not_in(occupied))
            round_row = await session.scalar(statement)
            if round_row is None:
                return None, None
            lease = await session.scalar(select(LoopCoordinatorLease).where(
                LoopCoordinatorLease.round_id == round_row.round_id,
            ).with_for_update())
            if lease is None or lease.expires_at <= now:
                return round_row, lease
            occupied.add(round_row.round_id)

    async def ownership_lost(self, claim: CoordinatorClaim, reason: str = "lease_renewal_failed") -> None:
        async with self._sessions.begin() as session:
            fence = await session.get(LoopCoordinatorFence, claim.round_id, with_for_update=True)
            token = int(claim.fencing_token) if claim.fencing_token.isdecimal() else 0
            terminal = "superseded" if fence is not None and fence.fencing_token > token else "interrupted"
            attempts = list(
                (
                    await session.scalars(
                        select(LoopPatrolAttempt)
                        .where(
                            LoopPatrolAttempt.round_id == claim.round_id,
                            LoopPatrolAttempt.status.in_(("pending", "running")),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            now = datetime.now(UTC)
            for attempt in attempts:
                attempt.status = terminal
                attempt.error = reason
                attempt.completed_at = now
            await LoopEventJournal().append(
                session,
                claim.loop_id,
                CanonicalEventDraft(
                    kind=f"patrol.session.{terminal}",
                    entity_type="patrol_session",
                    entity_id=attempts[-1].patrol_attempt_id if attempts else claim.round_id,
                    entity_revision=max(1, attempts[-1].attempt if attempts else token),
                    payload={"status": terminal, "reason": reason, "round_id": claim.round_id},
                    idempotency_key=f"ownership-lost:{claim.round_id}:{claim.fencing_token}",
                ),
            )

    async def maintain_rounds(self) -> tuple[str, ...]:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            loops = tuple((await session.scalars(select(AgentLoop).join(
                LoopRound, LoopRound.round_id == AgentLoop.current_round_id,
            ).where(
                AgentLoop.status == "running", or_(LoopRound.status == "settled",
                    AgentLoop.waiting_reason.startswith("round_consequences:"))
            ).with_for_update(skip_locked=True))).all())
            for loop in loops:
                current = await session.get(LoopRound, loop.current_round_id, with_for_update=True)
                if current is not None and current.status == "settled":
                    await self._continue_settled_round(session, loop, current)
                    continue
                if current is None or current.status != "running":
                    continue
                run = await session.scalar(select(DesktopRun).where(
                    DesktopRun.loop_id == loop.loop_id, DesktopRun.round_id == current.round_id,
                    DesktopRun.settled_at.is_not(None),
                ).order_by(DesktopRun.settled_at.desc()).limit(1))
                if run is not None:
                    from backend.app.desktop.run_orchestration.models import RunOutboxEvent

                    event = await session.scalar(select(RunOutboxEvent).where(
                        RunOutboxEvent.run_id == run.run_id,
                        RunOutboxEvent.event_type == "MainRunSettled",
                    ))
                    await self._advance_from_run(session, loop, current, run, event)
            terminated = await terminate_stalled_rounds(session, self._stall_limits, now, category="watchdog")
        if terminated:
            await self.release_rounds(terminated)
        return tuple(terminated)

    async def release_rounds(self, round_ids: Sequence[str]) -> int:
        if not round_ids:
            return 0
        async with self._sessions.begin() as session:
            result = await session.execute(delete(LoopCoordinatorLease).where(LoopCoordinatorLease.round_id.in_(list(round_ids))))
            return int(result.rowcount or 0)

    async def release(self, claim: CoordinatorClaim) -> bool:
        async with self._sessions.begin() as session:
            lease = await session.scalar(select(LoopCoordinatorLease).where(LoopCoordinatorLease.lease_id == claim.lease_id).with_for_update())
            if lease is None or lease.fencing_token != claim.fencing_token:
                return False
            await session.delete(lease)
            return True

    async def renew(self, claim: CoordinatorClaim) -> bool:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            lease = await session.scalar(select(LoopCoordinatorLease).where(LoopCoordinatorLease.lease_id == claim.lease_id).with_for_update())
            if lease is None or lease.fencing_token != claim.fencing_token or lease.expires_at <= now:
                return False
            lease.expires_at = now + timedelta(seconds=self._ttl)
            return True

    async def recover(self) -> int:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            rows = list((await session.scalars(select(LoopCoordinatorLease).where(LoopCoordinatorLease.expires_at <= now))).all())
            for row in rows:
                await session.delete(row)
            await self._recover_launching_directives(session)
            return len(rows)

    async def _recover_launching_directives(self, session: AsyncSession) -> int:
        directives = list(
            (
                await session.scalars(
                    select(LoopDirective)
                    .where(LoopDirective.status == "launching")
                    .order_by(LoopDirective.directive_id)
                    .with_for_update()
                )
            ).all()
        )
        retries_by_loop: dict[str, int] = {}
        for directive in directives:
            run = await session.scalar(
                select(DesktopRun)
                .where(DesktopRun.directive_id == directive.directive_id)
                .order_by(DesktopRun.created_at.desc())
                .limit(1)
            )
            if run is None:
                directive.status = "created"
                await self._directive_lifecycle.transition(session, directive.directive_id, "authorized", reason="process_restarted")
                retries_by_loop[directive.loop_id] = retries_by_loop.get(directive.loop_id, 0) + 1
                continue
            reconciliation = (run.workspace_result or {}).get("reconciliation") or {}
            if run.status == "interrupted" and reconciliation.get("retry_safe") is True:
                directive.status = "created"
                directive.launched_run_id = None
                await self._directive_lifecycle.transition(session, directive.directive_id, "authorized", reason="retry_safe_recovery")
                retries_by_loop[directive.loop_id] = retries_by_loop.get(directive.loop_id, 0) + 1
                continue
            directive.status = "launched" if run.status in {"pending", "running", "success"} else "blocked"
            directive.launched_run_id = run.run_id
            if directive.lifecycle_state == "delivering":
                if directive.status == "launched":
                    await self._directive_lifecycle.transition(session, directive.directive_id, "delivered", run_id=run.run_id, reason="recovered_delivery")
                else:
                    await self._directive_lifecycle.transition(session, directive.directive_id, "delivery_failed", run_id=run.run_id, reason=run.error or "recovered_failed_run")
            if directive.lifecycle_state == "delivered" and run.status in {"running", "success"}:
                await self._directive_lifecycle.transition(session, directive.directive_id, "run_started", run_id=run.run_id, reason="recovered_run")
        for loop_id, retries in retries_by_loop.items():
            usage = await session.get(LoopBudgetUsage, loop_id, with_for_update=True)
            if usage is not None:
                LoopUsageLedger.apply(usage, LoopUsageDelta(retries=retries))
        return len(directives)

    async def dispatch_ready(
        self,
        claim: CoordinatorClaim,
        dispatcher: LoopWaveDispatcher,
        concurrency: int,
    ) -> tuple[str, ...] | None:
        async with self._sessions() as session:
            round_row = await session.get(LoopRound, claim.round_id)
            loop = await session.get(AgentLoop, claim.loop_id)
            grant = await session.scalar(
                select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == claim.loop_id,
                    LoopDelegationGrant.revision == loop.authority_revision,
                )
            ) if loop else None
            usage = await session.get(LoopBudgetUsage, claim.loop_id) if loop else None
        if (loop is None or loop.status != "running" or round_row is None
                or loop.current_round_id != round_row.round_id or round_row.status not in {"ready", "running"}):
            return None
        usage_values = {
            field: int(getattr(usage, field, 0) or 0)
            for field in ("rounds", "model_calls", "input_tokens", "output_tokens", "retries", "lanes", "no_progress_count")
        }
        created_at = loop.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        usage_values["duration_seconds"] = max(0, int((datetime.now(UTC) - created_at).total_seconds()))
        usage_values["providers"] = configured_provider_count(loop.equipment or {})
        usage_values["contexts"] = int(
            await self._context_count(claim.loop_id)
        )
        budget = LoopBudgetGuard().evaluate(usage_values, grant.budgets if grant else {}, "dispatch")
        if grant is None or grant.status != "active" or budget.status == "exhausted":
            async with self._sessions.begin() as session:
                current_loop = await session.get(AgentLoop, claim.loop_id, with_for_update=True, populate_existing=True)
                current = await session.get(LoopRound, claim.round_id, with_for_update=True, populate_existing=True)
                if current is not None:
                    await terminate_round(session, current_loop, current, category="budget", reason="Loop hard budget 已耗尽，未启动新的 Run", allowed_statuses=("ready",))
            return ()
        configured = int((grant.budgets if grant else {}).get("max_concurrent_runs", concurrency))
        run_ids = ()
        try:
            run_ids = await dispatcher.dispatch(claim.loop_id, claim.round_id, min(concurrency, configured))
        finally:
            current = await self._record_dispatch_state(claim, run_ids)
        return run_ids if current else None

    async def _record_dispatch_state(self, claim: CoordinatorClaim, run_ids: tuple[str, ...]) -> bool:
        async with self._sessions.begin() as session:
            loop = await session.get(AgentLoop, claim.loop_id, with_for_update=True, populate_existing=True)
            current = await session.get(LoopRound, claim.round_id, with_for_update=True, populate_existing=True)
            if (current is None or loop is None or loop.status != "running"
                    or loop.current_round_id != current.round_id or current.status not in {"ready", "running"}):
                return False
            queued = int(
                await session.scalar(
                    select(func.count()).select_from(LoopDirective).where(
                        LoopDirective.round_id == claim.round_id,
                        LoopDirective.status.in_(("created", "launching")),
                    )
                )
                or 0
            )
            active = int(await session.scalar(select(func.count()).select_from(DesktopRun).where(
                DesktopRun.round_id == current.round_id, DesktopRun.status.in_(("pending", "running")))) or 0)
            current.status = "running" if run_ids or active else "ready" if queued else "error"
            loop.health = "waiting_runs" if run_ids or active else "dispatching" if queued else "degraded"
            from backend.app.desktop.agent_loop.lifecycle_events import LoopLifecycleEventRecorder
            from backend.app.desktop.agent_loop.round_events import RoundStateEventRecorder

            await RoundStateEventRecorder().record(session, current)
            await LoopLifecycleEventRecorder().record(session, loop)
        return True

    async def _context_count(self, loop_id: str) -> int:
        async with self._sessions() as session:
            return int(
                await session.scalar(
                    select(func.count()).select_from(LoopContextMembership).where(
                        LoopContextMembership.loop_id == loop_id,
                        LoopContextMembership.status != "discarded",
                    )
                ) or 0
            )

    async def handle_run_settled(self, event, session: AsyncSession) -> None:
        run = await session.get(DesktopRun, event.run_id)
        if run is None or run.loop_id is None or run.round_id is None:
            return
        await session.flush()
        loop = await session.get(AgentLoop, run.loop_id, with_for_update=True, populate_existing=True)
        round_row = await session.get(LoopRound, run.round_id, with_for_update=True, populate_existing=True)
        if round_row is None or loop is None:
            return
        if run.user_intent_id:
            intent = await session.get(LoopUserIntent, run.user_intent_id, with_for_update=True)
            if intent is not None and run.status == "error" and intent.delivery_state in {"accepted", "observed", "delivered"}:
                if intent.delivery_state in {"accepted", "observed"}:
                    await self._interventions.transition(session, intent.intent_id, "delivered", run_id=run.run_id)
                await self._interventions.transition(session, intent.intent_id, "delivery_failed", run_id=run.run_id, reason=run.error)
            elif intent is not None and intent.delivery_state == "run_started":
                await self._interventions.transition(
                    session,
                    intent.intent_id,
                    "settled" if run.status == "success" else "failed",
                    run_id=run.run_id,
                    reason=run.error,
                )
            if intent is not None and not run.directive_id:
                await self._user_run_events.settled(session, intent, run, getattr(event, "event_id", None))
        elif run.origin == "direct_user":
            await self._user_run_events.settled(session, None, run, getattr(event, "event_id", None))
        revision_id = ((event.payload or {}).get("context_revision") or {}).get("revision_id")
        if revision_id and run.origin_message_id:
            provenance = await session.scalar(select(MessageProvenance).where(MessageProvenance.message_id == run.origin_message_id).with_for_update())
            if provenance is not None:
                provenance.context_revision_id = revision_id
        if run.directive_id:
            directive = await session.get(LoopDirective, run.directive_id, with_for_update=True)
            if (directive is not None and directive.lifecycle_state in {"delivering", "delivered"}
                and run.status == "error" and run.parent_run_id is None
                and directive.launched_run_id in {None, run.run_id}):
                directive.status = "blocked"
                directive.queued_reason = run.error or "run_start_failed"
                await self._directive_lifecycle.transition(
                    session, directive.directive_id, "delivery_failed",
                    run_id=run.run_id, reason=directive.queued_reason,
                    caused_by_event_id=getattr(event, "event_id", None),
                )
            elif directive is not None and directive.lifecycle_state == "run_started" and directive.launched_run_id == run.run_id:
                await self._directive_lifecycle.transition(
                    session,
                    directive.directive_id,
                    "settled" if run.status == "success" else "failed",
                    run_id=run.run_id,
                    reason=run.error,
                    caused_by_event_id=getattr(event, "event_id", None),
                )
            if directive is not None and (directive.loop_id, directive.round_id) == (run.loop_id, run.round_id):
                await self._directive_causality.run_settled(session, directive, run, getattr(event, "event_id", None))
        resolution_state = None
        if self._compression_resolutions is not None:
            resolution_state = await self._compression_resolutions.settle_run(session, event, run, loop)
        if resolution_state == "failed":
            return
        usage = await session.get(LoopBudgetUsage, loop.loop_id, with_for_update=True)
        reported = (run.equipment or {}).get("_loop_settlement_usage_reported")
        audited = await session.scalar(select(ModelAttemptAudit.attempt_id).where(
            ModelAttemptAudit.run_id == run.run_id, ModelAttemptAudit.usage_accounting["loop_id"].astext == loop.loop_id,
        ).limit(1))
        if usage is not None and not reported and audited is None:
            LoopUsageLedger.apply(usage, LoopUsageDelta(model_calls=int(run.model_call_count or 0),
                input_tokens=int(run.prompt_input_tokens or 0), output_tokens=int(run.prompt_output_tokens or 0)))
            run.equipment = {**(run.equipment or {}), "_loop_settlement_usage_reported": True}
        if loop.status != "running":
            return
        if round_row.status != "running":
            return
        await self._advance_from_run(session, loop, round_row, run, event)

    async def _advance_from_run(self, session, loop, round_row, run, event):
        if loop.status != "running" or loop.current_round_id != round_row.round_id or round_row.status != "running":
            return
        usage = await session.get(LoopBudgetUsage, loop.loop_id, with_for_update=True)
        projected_gate = None
        if self._compression_gates is not None and event is not None:
            projected_gate = await self._compression_gates.project_settled(session, event, run, loop)
        active = await session.scalar(select(func.count()).select_from(DesktopRun).where(DesktopRun.round_id == run.round_id, DesktopRun.status.in_(["pending", "running"])))
        if active:
            round_row.status = "running"
            loop.health = "waiting_runs"
            await self._record_live_state(session, loop, round_row)
            return
        queued = await session.scalar(select(func.count()).select_from(LoopDirective).where(LoopDirective.round_id == run.round_id, LoopDirective.status == "created"))
        if queued:
            round_row.status = "ready"
            loop.health = "dispatching"
            await self._record_live_state(session, loop, round_row)
            sequence = int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop.loop_id)) or 0) + 1
            session.add(LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop.loop_id, sequence=sequence, event_type="LoopWaveReady", payload={"round_id": round_row.round_id, "remaining_directives": queued}, idempotency_key=f"wave-ready:{run.run_id}"))
            return
        from backend.app.desktop.agent_loop.rounds import settle_round

        if not await settle_round(session, loop, round_row):
            if loop.status == "running":
                loop.health = "waiting_runs"
            await self._record_live_state(session, loop, round_row)
            return
        await self._complete_patrol_session(session, round_row.round_id, run.run_id)
        if projected_gate is not None and not projected_gate.delegable:
            return
        await self._continue_settled_round(session, loop, round_row, run)

    @staticmethod
    async def _record_live_state(session, loop, round_row):
        from backend.app.desktop.agent_loop.lifecycle_events import LoopLifecycleEventRecorder
        from backend.app.desktop.agent_loop.round_events import RoundStateEventRecorder

        await RoundStateEventRecorder().record(session, round_row)
        await LoopLifecycleEventRecorder().record(session, loop)

    async def _continue_settled_round(self, session, loop, round_row, run=None):
        from backend.app.desktop.agent_loop.rounds import advance_settled_round

        next_round = await advance_settled_round(session, loop, round_row)
        if next_round is None:
            return
        sequence = int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop.loop_id)) or 0) + 1
        session.add(LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop.loop_id, sequence=sequence,
            event_type="RoundObserved", payload={"round_id": next_round.round_id, "source_round_id": round_row.round_id,
            "settled_run_id": run.run_id if run else None}, idempotency_key=f"round-settled:{round_row.round_id}"))

    async def _complete_patrol_session(self, session: AsyncSession, round_id: str, run_id: str) -> None:
        patrol = await session.scalar(
            select(LoopPatrolSession)
            .where(LoopPatrolSession.round_id == round_id, LoopPatrolSession.status == "active")
            .with_for_update()
        )
        if patrol is None:
            return
        await self._patrol_sessions.transition(
            session,
            patrol.session_id,
            PatrolPhase.COMPLETED,
            PatrolActivity(summary="Context Run 证据已返回，本轮 Patrol 等待结束"),
            terminal_outcome={"status": "completed", "evidence_run_id": run_id},
        )

    @staticmethod
    async def _current_frontier_hash(session: AsyncSession, loop_id: str) -> str:
        value = await current_frontier_hash(session, loop_id)
        if value is None:
            raise ValueError("当前 Loop 没有活跃 Context frontier")
        return value


class LoopCoordinatorRuntime:
    def __init__(
        self,
        coordinator: LoopCoordinator,
        run_events: RunOutboxConsumer,
        dispatcher: LoopWaveDispatcher | None = None,
        orchestrator: RoundOrchestratorPort | None = None,
        workers: WorkerRuntimePort | None = None,
        recovery: RecoveryPort | None = None,
        compression_resolutions: CompressionResolutionPort | None = None,
        poll_seconds: float = 1.0,
        maintenance: RoundMaintenancePort | None = None,
        max_concurrent_loops: int = 4,
        portfolio_publications: SupervisedQueuePort | None = None,
        context_runs: SupervisedQueuePort | None = None,
        fact_projector: SupervisedQueuePort | None = None,
        task_progress=None,
    ) -> None:
        self._coordinator = coordinator
        self._run_events = run_events
        self._dispatcher = dispatcher
        self._orchestrator = orchestrator
        self._workers = workers
        self._recovery = recovery
        self._compression_resolutions = compression_resolutions
        self._maintenance = maintenance
        self._portfolio_publications = portfolio_publications
        self._context_runs = context_runs
        self._fact_projector = fact_projector
        self._task_progress = task_progress
        self._poll_seconds = poll_seconds
        self._max_concurrent_loops = max(1, max_concurrent_loops)
        self._supervisor: LoopSupervisor | None = None
        self._registry = LoopSupervisorRegistry(self._components_for_loop)
        self._round_slots = asyncio.Semaphore(self._max_concurrent_loops)

    @property
    def supervised_loop_ids(self) -> tuple[str, ...]:
        return self._registry.loop_ids

    async def start(self) -> None:
        if self._supervisor is not None:
            return
        if self._recovery is not None:
            await self._recovery.reconcile()
        else:
            await self._run_events.recover()
            await self._coordinator.recover()
        if self._compression_resolutions is not None:
            await self._compression_resolutions.reconcile()
        if self._portfolio_publications is not None:
            recover = getattr(self._portfolio_publications, "recover", None)
            if recover is not None:
                await recover()
        if self._fact_projector is not None:
            start = getattr(self._fact_projector, "start", None)
            if start is not None:
                await start()
        await self._reconcile_supervisors()
        components = [SupervisorComponent("loop_registry", self._reconcile_supervisors, self._poll_seconds)]
        if self._task_progress is not None:
            components.append(SupervisorComponent("task_progress", self._task_progress.drain, self._poll_seconds))
        if self._compression_resolutions is not None:
            components.append(SupervisorComponent("compression_resolutions", self._compression_resolutions.drain, self._poll_seconds))
        if self._maintenance is not None:
            components.append(SupervisorComponent("maintenance", self._maintenance.maintain_rounds, self._poll_seconds))
        self._supervisor = LoopSupervisor("agent-loop-admission", components)
        await self._supervisor.start()

    async def close(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.close()
            self._supervisor = None
        await self._registry.close()
        for component in (self._workers, self._portfolio_publications, self._context_runs, self._fact_projector):
            close = getattr(component, "close", None)
            if close is not None:
                await close()

    def _components_for_loop(self, loop_id: str) -> tuple[SupervisorComponent, ...]:
        components = [
            SupervisorComponent("run_events", lambda: self._drain_run_events(loop_id), self._poll_seconds),
            SupervisorComponent("rounds", lambda: self._process_round(loop_id), self._poll_seconds),
        ]
        if self._workers is not None:
            components.append(SupervisorComponent("workers", lambda: self._workers.drain(loop_id), self._poll_seconds))
        if self._portfolio_publications is not None:
            components.append(SupervisorComponent("portfolio_publications", lambda: self._portfolio_publications.drain(loop_id), self._poll_seconds))
        if self._context_runs is not None:
            components.append(SupervisorComponent("context_runs", lambda: self._context_runs.drain(loop_id), self._poll_seconds))
        if self._fact_projector is not None:
            components.append(SupervisorComponent("fact_projector", lambda: self._fact_projector.project_loop(loop_id), self._poll_seconds))
        return tuple(components)

    async def _reconcile_supervisors(self) -> tuple[str, ...]:
        return await self._registry.reconcile(await self._coordinator.running_loop_ids())

    async def _drain_run_events(self, loop_id: str) -> int:
        return await self._run_events.drain(
            f"agent-loop-run-events:{loop_id}",
            self._coordinator.handle_run_settled,
            loop_id=loop_id,
        )

    async def _process_round(self, loop_id: str) -> None:
        async with self._round_slots:
            claim = await self._coordinator.claim_for_loop(loop_id, f"agent-loop-coordinator:{loop_id}")
            if claim is not None:
                await self._execute_claim(claim)

    async def _execute_claim(self, claim: CoordinatorClaim) -> None:
        work = asyncio.create_task(self._perform_claim(claim))
        renew = getattr(self._coordinator, "renew", None)
        renewal = asyncio.create_task(self._renew_claim(claim, renew)) if renew is not None else None
        try:
            if renewal is None:
                await work
                return
            done, _ = await asyncio.wait((work, renewal), return_when=asyncio.FIRST_COMPLETED)
            if renewal in done and renewal.result() is False and not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                ownership_lost = getattr(self._coordinator, "ownership_lost", None)
                if ownership_lost is not None:
                    await ownership_lost(claim)
                return
            if work in done:
                await work
        finally:
            if renewal is not None:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            await self._coordinator.release(claim)

    async def _perform_claim(self, claim: CoordinatorClaim) -> None:
        if self._orchestrator is not None:
            result = await self._orchestrator.process(claim)
            if result is not None:
                return
        if self._dispatcher is not None and self._context_runs is None:
            await self._coordinator.dispatch_ready(claim, self._dispatcher, 4)

    async def _renew_claim(self, claim: CoordinatorClaim, renew) -> bool:
        interval = float(getattr(self._coordinator, "renewal_interval", max(self._poll_seconds, 0.1)))
        while True:
            await asyncio.sleep(interval)
            if not await renew(claim):
                return False
