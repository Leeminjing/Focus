r"""本文件对外提供 AgentLoopRecovery 与 LoopRecoveryReport。

输入为重启后的持久 decision lease、Worker/Patrol attempt、shadow publication、directive、Run outbox 和
workspace lease 状态；输出为可重试、需观察或已恢复事件的计数。具体工作流为只重置提交前计算状态，
保留已 commit 权威事实，Writer 副作用进入 observation 而不盲重跑。示例：`await recovery.reconcile()`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.models import LoopDirective, LoopPatrolAttempt, LoopWorkerRequest
from backend.app.desktop.context_curation.models import PortfolioLaneCandidate, PortfolioPublicationAttempt
from backend.app.desktop.run_orchestration import RunOutboxConsumer
from backend.app.desktop.workspace_coordination.models import WorkspaceLease


@dataclass(frozen=True, slots=True)
class LoopRecoveryReport:
    coordinator_leases: int
    patrol_attempts: int
    worker_attempts: int
    shadow_candidates: int
    directives: int
    workspace_leases: int
    run_events: int


class AgentLoopRecovery:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], coordinator: LoopCoordinator, run_events: RunOutboxConsumer) -> None:
        self._sessions = sessions
        self._coordinator = coordinator
        self._run_events = run_events

    async def reconcile(self) -> LoopRecoveryReport:
        coordinator_leases = await self._coordinator.recover()
        run_events = await self._run_events.recover()
        async with self._sessions.begin() as session:
            patrol = await session.execute(update(LoopPatrolAttempt).where(LoopPatrolAttempt.status == "running").values(status="error", error="process_restarted"))
            workers = await session.execute(update(LoopWorkerRequest).where(LoopWorkerRequest.status == "running").values(status="pending"))
            shadows = await session.execute(update(PortfolioLaneCandidate).where(PortfolioLaneCandidate.status == "preparing").values(status="pending", error="process_restarted"))
            launching = list((await session.scalars(select(LoopDirective.directive_id).where(LoopDirective.status == "launching"))).all())
            leases = await session.execute(update(WorkspaceLease).where(WorkspaceLease.status == "active", WorkspaceLease.expires_at <= datetime.now(UTC)).values(status="expired"))
            await session.execute(update(PortfolioPublicationAttempt).where(PortfolioPublicationAttempt.status == "publishing").values(status="recovery_required"))
            return LoopRecoveryReport(coordinator_leases, int(patrol.rowcount or 0), int(workers.rowcount or 0), int(shadows.rowcount or 0), len(launching), int(leases.rowcount or 0), run_events)
