r"""本文件对外提供 LoopCoordinator、CoordinatorClaim 与 LoopCoordinatorRuntime。

输入为数据库中的 running Loop、未决 round、Worker/Run/outbox 事实和 coordinator identity；输出为带
lease 的唯一 round claim 与恢复计数。具体工作流为 skip-locked 领取、fencing stale attempt、由数据库
状态推进 health；Runtime 启动时执行完整 AgentLoopRecovery，随后消费持久 outbox 并通过 Coordinator
原子派发 ready wave，进程内 wake 只缩短延迟。示例：`runtime = LoopCoordinatorRuntime(...)`。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import uuid
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import AgentLoop, LoopAction, LoopBudgetUsage, LoopContextMembership, LoopCoordinatorLease, LoopDirective, LoopEventOutbox, LoopRound, LoopWorkerRequest, MessageProvenance
from backend.app.desktop.agent_loop.budgets import LoopBudgetGuard, configured_provider_count, no_progress_fingerprint
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger
from backend.app.desktop.agent_loop.models import LoopDelegationGrant
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from backend.app.desktop.run_orchestration import RunOutboxConsumer
from backend.app.desktop.agent_loop.dispatch import LoopWaveDispatcher


logger = logging.getLogger(__name__)


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


class LoopCoordinator:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], ttl_seconds: int = 60) -> None:
        self._sessions = sessions
        self._ttl = ttl_seconds

    async def claim(self, owner_id: str) -> CoordinatorClaim | None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            await session.execute(update(LoopCoordinatorLease).where(LoopCoordinatorLease.expires_at <= now).values(expires_at=now))
            round_row = await session.scalar(
                select(LoopRound)
                .join(AgentLoop, AgentLoop.loop_id == LoopRound.loop_id)
                .where(AgentLoop.status == "running", LoopRound.status.in_(["observed", "publishing", "adopting", "ready"]))
                .order_by(LoopRound.started_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if round_row is None:
                return None
            lease = await session.scalar(select(LoopCoordinatorLease).where(LoopCoordinatorLease.round_id == round_row.round_id).with_for_update())
            if lease is not None and lease.expires_at > now:
                return None
            if lease is None:
                lease = LoopCoordinatorLease(lease_id=uuid.uuid4().hex, round_id=round_row.round_id, owner_id=owner_id, fencing_token=uuid.uuid4().hex, expires_at=now + timedelta(seconds=self._ttl))
                session.add(lease)
            else:
                lease.owner_id = owner_id
                lease.fencing_token = uuid.uuid4().hex
                lease.expires_at = now + timedelta(seconds=self._ttl)
            loop = await session.get(AgentLoop, round_row.loop_id)
            if loop is not None:
                loop.health = "deciding" if round_row.status == "observed" else loop.health
            return CoordinatorClaim(lease.lease_id, round_row.loop_id, round_row.round_id, lease.fencing_token)

    async def release(self, claim: CoordinatorClaim) -> bool:
        async with self._sessions.begin() as session:
            lease = await session.scalar(select(LoopCoordinatorLease).where(LoopCoordinatorLease.lease_id == claim.lease_id).with_for_update())
            if lease is None or lease.fencing_token != claim.fencing_token:
                return False
            await session.delete(lease)
            return True

    async def recover(self) -> int:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            rows = list((await session.scalars(select(LoopCoordinatorLease).where(LoopCoordinatorLease.expires_at <= now))).all())
            for row in rows:
                await session.delete(row)
            await self._recover_launching_directives(session)
            return len(rows)

    @staticmethod
    async def _recover_launching_directives(session: AsyncSession) -> int:
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
                retries_by_loop[directive.loop_id] = retries_by_loop.get(directive.loop_id, 0) + 1
                continue
            reconciliation = (run.workspace_result or {}).get("reconciliation") or {}
            if run.status == "interrupted" and reconciliation.get("retry_safe") is True:
                directive.status = "created"
                directive.launched_run_id = None
                retries_by_loop[directive.loop_id] = retries_by_loop.get(directive.loop_id, 0) + 1
                continue
            directive.status = "launched" if run.status in {"pending", "running", "success"} else "blocked"
            directive.launched_run_id = run.run_id
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
        if loop is None or round_row is None or round_row.status != "ready":
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
        if grant is None or budget.status == "exhausted":
            async with self._sessions.begin() as session:
                current = await session.get(LoopRound, claim.round_id, with_for_update=True)
                current_loop = await session.get(AgentLoop, claim.loop_id, with_for_update=True)
                if current is not None and current.status == "ready":
                    current.status = "error"
                if current_loop is not None and current_loop.status == "running":
                    current_loop.status = "waiting_user"
                    current_loop.health = "degraded"
                    current_loop.waiting_reason = "Loop hard budget 已耗尽，未启动新的 Run"
            return ()
        configured = int((grant.budgets if grant else {}).get("max_concurrent_runs", concurrency))
        run_ids = await dispatcher.dispatch(claim.loop_id, claim.round_id, min(concurrency, configured))
        async with self._sessions.begin() as session:
            current = await session.get(LoopRound, claim.round_id, with_for_update=True)
            loop = await session.get(AgentLoop, claim.loop_id, with_for_update=True)
            if current is None or loop is None or current.status != "ready":
                return None
            current.status = "running" if run_ids else "error"
            loop.health = "waiting_runs" if run_ids else "degraded"
        return run_ids

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
        revision_id = ((event.payload or {}).get("context_revision") or {}).get("revision_id")
        if revision_id and run.origin_message_id:
            provenance = await session.scalar(select(MessageProvenance).where(MessageProvenance.message_id == run.origin_message_id).with_for_update())
            if provenance is not None:
                provenance.context_revision_id = revision_id
        round_row = await session.get(LoopRound, run.round_id, with_for_update=True)
        loop = await session.get(AgentLoop, run.loop_id, with_for_update=True)
        if round_row is None or loop is None or loop.status != "running" or round_row.status != "running":
            return
        usage = await session.get(LoopBudgetUsage, loop.loop_id, with_for_update=True)
        if usage is not None:
            LoopUsageLedger.apply(
                usage,
                LoopUsageDelta(
                    model_calls=int(run.model_call_count or 0),
                    input_tokens=int(run.prompt_input_tokens or 0),
                    output_tokens=int(run.prompt_output_tokens or 0),
                ),
            )
        active = await session.scalar(select(func.count()).select_from(DesktopRun).where(DesktopRun.round_id == run.round_id, DesktopRun.status.in_(["pending", "running"])))
        if active:
            round_row.status = "running"
            loop.health = "waiting_runs"
            return
        queued = await session.scalar(select(func.count()).select_from(LoopDirective).where(LoopDirective.round_id == run.round_id, LoopDirective.status == "created"))
        if queued:
            round_row.status = "ready"
            loop.health = "dispatching"
            sequence = int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop.loop_id)) or 0) + 1
            session.add(LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop.loop_id, sequence=sequence, event_type="LoopWaveReady", payload={"round_id": round_row.round_id, "remaining_directives": queued}, idempotency_key=f"wave-ready:{run.run_id}"))
            return
        round_row.status = "settled"
        round_row.settled_at = datetime.now(UTC)
        number = int(await session.scalar(select(func.max(LoopRound.number)).where(LoopRound.loop_id == loop.loop_id)) or 0) + 1
        frontier_hash = await self._current_frontier_hash(session, loop.loop_id)
        slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == loop.workspace_id, WorkspaceSlot.kind == "authoritative", WorkspaceSlot.lifecycle != "deleted"))
        workspace_revision = slot.revision if slot is not None else int((run.workspace_result or {}).get("revision") or round_row.workspace_revision)
        next_round = LoopRound(round_id=uuid.uuid4().hex, loop_id=loop.loop_id, number=number, authority_revision=loop.authority_revision, goal_revision=loop.goal_revision, frontier_hash=frontier_hash, workspace_revision=workspace_revision)
        session.add(next_round)
        loop.current_round_id = next_round.round_id
        loop.health = "observing"
        loop.revision += 1
        if usage is not None:
            usage.rounds += 1
            if run.status == "success":
                usage.no_progress_count = 0
                usage.no_progress_fingerprint = None
            else:
                action = await session.get(LoopAction, run.action_id) if run.action_id else None
                progress = no_progress_fingerprint(
                    status=run.status,
                    error=run.error,
                    workspace_fingerprint=(run.workspace_result or {}).get("fingerprint"),
                    action_type=action.action_type if action else None,
                )
                usage.no_progress_count = usage.no_progress_count + 1 if usage.no_progress_fingerprint == progress else 1
                usage.no_progress_fingerprint = progress
        sequence = int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop.loop_id)) or 0) + 1
        session.add(LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop.loop_id, sequence=sequence, event_type="RoundObserved", payload={"round_id": next_round.round_id, "settled_run_id": run.run_id}, idempotency_key=f"run-settled:{run.run_id}"))

    @staticmethod
    async def _current_frontier_hash(session: AsyncSession, loop_id: str) -> str:
        memberships = list((await session.scalars(select(LoopContextMembership).where(LoopContextMembership.loop_id == loop_id, LoopContextMembership.status == "active").order_by(LoopContextMembership.membership_id))).all())
        frontier = []
        for membership in memberships:
            context = await session.get(DesktopThread, membership.context_id)
            frontier.append({"context_id": membership.context_id, "revision_id": context.current_revision_id if context else None})
        payload = json.dumps(frontier, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LoopCoordinatorRuntime:
    def __init__(self, coordinator: LoopCoordinator, run_events: RunOutboxConsumer, dispatcher: LoopWaveDispatcher | None = None, orchestrator: RoundOrchestratorPort | None = None, workers: WorkerRuntimePort | None = None, recovery: RecoveryPort | None = None, poll_seconds: float = 1.0) -> None:
        self._coordinator = coordinator
        self._run_events = run_events
        self._dispatcher = dispatcher
        self._orchestrator = orchestrator
        self._workers = workers
        self._recovery = recovery
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        if self._recovery is not None:
            await self._recovery.reconcile()
        else:
            await self._run_events.recover()
            await self._coordinator.recover()
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._run_events.drain(
                    "agent-loop-coordinator", self._coordinator.handle_run_settled
                )
                if self._workers is not None:
                    await self._workers.drain()
                await self._process_round()
            except Exception:
                logger.exception("Agent Loop coordinator 消费 Run 事件失败")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

    async def _process_round(self) -> None:
        claim = await self._coordinator.claim("agent-loop-coordinator")
        if claim is None:
            return
        try:
            if self._orchestrator is not None:
                result = await self._orchestrator.process(claim)
                if result is not None:
                    return
            if self._dispatcher is not None:
                await self._coordinator.dispatch_ready(claim, self._dispatcher, 4)
        finally:
            await self._coordinator.release(claim)
