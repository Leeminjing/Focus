r"""本文件对外提供 LoopRuntimeConvergence。

输入为已锁定并切换到 paused/stopped 的 AgentLoop、当前事务和终止原因；输出为需要通知执行器取消的 Run id。
具体工作流为同一事务终结未提交 round/decision、取消 Curator 与 directive、打断活动 Run、记录 Patrol 终态，
再追加一个不覆盖 Loop 投影主体的规范活动事件。示例：`run_ids = await convergence.converge(session, loop, "loop_paused")`。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.curator_assignments import CuratorAssignmentRepository
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopDecision,
    LoopDirective,
    LoopPatrolAttempt,
    LoopRound,
    LoopWorkerRequest,
)
from backend.app.desktop.agent_loop.patrol_session_models import LoopCuratorAssignment, LoopPatrolSession
from backend.app.desktop.agent_loop.patrol_session_repository import PatrolSessionRepository
from backend.app.desktop.agent_loop.patrol_session_state import PatrolActivity, PatrolPhase
from backend.app.desktop.models import DesktopRun


class LoopRuntimeConvergence:
    def __init__(self) -> None:
        self._journal = LoopEventJournal()
        self._curators = CuratorAssignmentRepository()
        self._patrol_sessions = PatrolSessionRepository()
        self._directives = DirectiveLifecycleRepository()

    async def converge(self, session: AsyncSession, loop: AgentLoop, reason: str) -> tuple[str, ...]:
        now = datetime.now(UTC)
        await session.execute(
            update(LoopRound)
            .where(
                LoopRound.loop_id == loop.loop_id,
                LoopRound.status.in_(("observed", "curated", "ready", "running", "waiting_workers", "publishing", "adopting")),
            )
            .values(status="superseded", settled_at=now)
        )
        await session.execute(
            update(LoopDecision)
            .where(
                LoopDecision.loop_id == loop.loop_id,
                LoopDecision.status.in_(("pending", "publishing", "publishing_run", "adopting")),
            )
            .values(status="superseded", queued_reason=reason, rejection={"reason": reason})
        )
        await session.execute(
            update(LoopWorkerRequest)
            .where(
                LoopWorkerRequest.loop_id == loop.loop_id,
                LoopWorkerRequest.status.in_(("pending", "running")),
            )
            .values(status="cancelled", result={"reason": reason}, completed_at=now)
        )
        curator_assignments = tuple(
            (
                await session.scalars(
                    select(LoopCuratorAssignment)
                    .where(
                        LoopCuratorAssignment.loop_id == loop.loop_id,
                        LoopCuratorAssignment.state.in_(("queued", "reading", "analyzing", "proposed")),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for assignment in curator_assignments:
            await self._curators.transition(
                session,
                assignment.assignment_id,
                "cancelled",
                "Loop 收敛时取消 Curator assignment",
                failure=reason,
            )
        patrol_sessions = tuple(
            (
                await session.scalars(
                    select(LoopPatrolSession)
                    .where(LoopPatrolSession.loop_id == loop.loop_id, LoopPatrolSession.status == "active")
                    .with_for_update()
                )
            ).all()
        )
        for patrol_session in patrol_sessions:
            await self._patrol_sessions.transition(
                session,
                patrol_session.session_id,
                PatrolPhase.INTERRUPTED,
                PatrolActivity(summary="Loop 运行已中断"),
                terminal_outcome={"status": "interrupted", "reason": reason},
            )
        await self._directives.cancel_active(session, loop.loop_id, reason)
        await session.execute(
            update(LoopPatrolAttempt)
            .where(
                LoopPatrolAttempt.loop_id == loop.loop_id,
                LoopPatrolAttempt.status.in_(("pending", "running")),
            )
            .values(status="interrupted", error=reason, completed_at=now)
        )
        run_ids = tuple(
            (
                await session.scalars(
                    select(DesktopRun.run_id).where(
                        DesktopRun.loop_id == loop.loop_id,
                        DesktopRun.status.in_(("pending", "running")),
                    )
                )
            ).all()
        )
        if run_ids:
            await session.execute(
                update(DesktopRun)
                .where(DesktopRun.run_id.in_(run_ids))
                .values(status="interrupted", error=reason, settled_at=now)
            )
        await self._journal.append(
            session,
            loop.loop_id,
            CanonicalEventDraft(
                kind="loop.runtime.converged",
                entity_type="runtime_work",
                entity_id=f"{loop.loop_id}:{loop.revision}",
                entity_revision=1,
                payload={"status": loop.status, "reason": reason, "cancelled_runs": len(run_ids)},
                idempotency_key=f"runtime-convergence:{loop.loop_id}:{loop.revision}",
            ),
        )
        return run_ids
