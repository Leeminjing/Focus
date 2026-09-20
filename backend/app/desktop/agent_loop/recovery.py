r"""本文件对外提供 AgentLoopRecovery 与 LoopRecoveryReport。

输入为重启后的持久 decision lease、Worker/Patrol attempt、停滞 round、shadow publication、directive、Run
outbox 和 workspace lease 状态；输出为可重试、需观察、已恢复与已收敛 round 的计数。具体工作流为只重置提交前
计算状态、保留已 commit 权威事实、收敛已不可能推进的 round（已有落定 decision 或越过无进展界限）并释放其
租约，Worker 仅在自己的持久最大尝试数内重排队，Writer 副作用进入 observation 而不盲重跑。示例：`await recovery.reconcile()`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDirective, LoopPatrolAttempt, LoopWorkerRequest
from backend.app.desktop.agent_loop.rounds import terminate_stalled_rounds
from backend.app.desktop.context_curation.models import PortfolioLaneCandidate, PortfolioPublicationAttempt
from backend.app.desktop.run_orchestration import RunOutboxConsumer
from backend.app.desktop.workspace_coordination.models import WorkspaceLease
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger


@dataclass(frozen=True, slots=True)
class LoopRecoveryReport:
    coordinator_leases: int
    patrol_attempts: int
    worker_attempts: int
    shadow_candidates: int
    directives: int
    workspace_leases: int
    run_events: int
    stalled_rounds: int


class AgentLoopRecovery:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], coordinator: LoopCoordinator, run_events: RunOutboxConsumer) -> None:
        self._sessions = sessions
        self._coordinator = coordinator
        self._run_events = run_events

    async def reconcile(self) -> LoopRecoveryReport:
        async with self._sessions() as session:
            launching_directives = int(
                await session.scalar(
                    select(func.count()).select_from(LoopDirective).where(LoopDirective.status == "launching")
                )
                or 0
            )
        coordinator_leases = await self._coordinator.recover()
        run_events = await self._run_events.recover()
        async with self._sessions.begin() as session:
            patrol_rows = list((await session.scalars(select(LoopPatrolAttempt).where(LoopPatrolAttempt.status == "running").with_for_update())).all())
            worker_rows = list((await session.scalars(select(LoopWorkerRequest).where(LoopWorkerRequest.status == "running").with_for_update())).all())
            retries_by_loop: dict[str, int] = {}
            for row in patrol_rows:
                row.status = "error"
                row.error = "process_restarted"
                row.completed_at = datetime.now(UTC)
                retries_by_loop[row.loop_id] = retries_by_loop.get(row.loop_id, 0) + 1
            for row in worker_rows:
                loop = await session.get(AgentLoop, row.loop_id)
                if loop is None or loop.status != "running":
                    row.status = "cancelled"
                    row.result = {"reason": "process_restarted_while_loop_inactive"}
                    row.completed_at = datetime.now(UTC)
                elif row.attempt < row.max_attempts:
                    row.status = "pending"
                    row.attempt += 1
                    row.retry_identity = f"worker:{row.worker_request_id}:attempt:{row.attempt}"
                    retries_by_loop[row.loop_id] = retries_by_loop.get(row.loop_id, 0) + 1
                else:
                    row.status = "error"
                    row.result = {"error": "process_restarted", "retrying": False}
                    row.completed_at = datetime.now(UTC)
            for loop_id, retries in retries_by_loop.items():
                usage = await session.get(LoopBudgetUsage, loop_id, with_for_update=True)
                if usage is not None:
                    LoopUsageLedger.apply(usage, LoopUsageDelta(retries=retries))
            stalled_rounds = await terminate_stalled_rounds(
                session,
                self._coordinator.stall_limits,
                datetime.now(UTC),
                category="recovery",
            )
            shadows = await session.execute(update(PortfolioLaneCandidate).where(PortfolioLaneCandidate.status == "preparing").values(status="pending", error="process_restarted"))
            leases = await session.execute(update(WorkspaceLease).where(WorkspaceLease.status == "active", WorkspaceLease.expires_at <= datetime.now(UTC)).values(status="expired"))
            await session.execute(update(PortfolioPublicationAttempt).where(PortfolioPublicationAttempt.status == "publishing").values(status="recovery_required"))
        await self._coordinator.release_rounds(stalled_rounds)
        return LoopRecoveryReport(coordinator_leases, len(patrol_rows), len(worker_rows), int(shadows.rowcount or 0), launching_directives, int(leases.rowcount or 0), run_events, len(stalled_rounds))
