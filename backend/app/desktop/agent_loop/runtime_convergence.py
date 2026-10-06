r"""本文件对外提供 LoopRuntimeConvergence。

输入为已锁定且撤销旧执行资格的 AgentLoop、当前事务和终止原因；输出为提交后需要通知执行器取消的 Run id。
具体工作流为同一事务终结未提交 round/decision、取消 Curator 与 directive、打断活动 Run、记录 Patrol 终态，
业务终止不填写 Run settled_at，消费与工作区清理由真实 finalizer 完成；Round 终态同事务发布规范事件，再追加一个不覆盖 Loop 投影主体的规范活动事件。
示例：`run_ids = await convergence.converge(session, loop, "loop_paused")`。
已授权直接用户消息随其 Directive 取消；尚未交付的消息可跨暂停或新发言保留，撤权/终止/Mission 修订明确取消，不让迟到结算恢复。
活动等待同事务通过既有等待服务终结；新发言、Mission 或授权变更使旧请求 superseded，终止/暂停使其 cancelled。
历史请求与规范事件保留，晚到响应不能再控制后继 Round，也不能阻塞新等待请求的唯一活动槽。
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
    LoopUserIntent,
)
from backend.app.desktop.agent_loop.patrol_session_models import LoopCuratorAssignment, LoopPatrolSession
from backend.app.desktop.agent_loop.patrol_session_repository import PatrolSessionRepository
from backend.app.desktop.agent_loop.patrol_session_state import PatrolActivity, PatrolPhase
from backend.app.desktop.agent_loop.wait_requests import LoopWaitRequestService
from backend.app.desktop.models import DesktopRun


class LoopRuntimeConvergence:
    def __init__(self) -> None:
        self._journal = LoopEventJournal()
        self._curators = CuratorAssignmentRepository()
        self._patrol_sessions = PatrolSessionRepository()
        self._directives = DirectiveLifecycleRepository()
        self._waits = LoopWaitRequestService()

    async def converge(self, session: AsyncSession, loop: AgentLoop, reason: str) -> tuple[str, ...]:
        now = datetime.now(UTC)
        active_wait = await self._waits.active(session, loop.loop_id, lock=True)
        if active_wait is not None:
            await self._waits.cancel(session, active_wait.request_id,
                superseded=reason in {"direct_user_message", "mission_revision", "authority_changed"})
            loop.waiting_reason = None
        terminated_rounds = tuple((await session.scalars(
            update(LoopRound)
            .where(
                LoopRound.loop_id == loop.loop_id,
                LoopRound.status.in_(("observed", "curated", "ready", "running", "waiting_workers", "publishing", "adopting")),
            )
            .values(status="superseded", settled_at=now)
            .returning(LoopRound)
        )).all())
        from backend.app.desktop.agent_loop.round_events import RoundStateEventRecorder

        for round_row in terminated_rounds:
            await RoundStateEventRecorder().record(session, round_row)
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
        await self._cancel_user_deliveries(session, loop, reason)
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
                .values(status="interrupted", error=reason)
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

    @staticmethod
    async def _cancel_user_deliveries(session, loop, reason):
        from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository

        intents = tuple((await session.scalars(select(LoopUserIntent).where(
            LoopUserIntent.loop_id == loop.loop_id, LoopUserIntent.intent_kind == "direct_message",
            LoopUserIntent.delivery_state.in_(("accepted", "observed", "delivered", "run_started"))
        ).with_for_update())).all())
        for intent in intents:
            directive = await session.scalar(select(LoopDirective.directive_id).where(
                LoopDirective.origin_kind == "direct_user", LoopDirective.correlation_id == intent.intent_id))
            if directive is not None or reason not in {"direct_user_message", "loop_paused"}:
                await InterventionLifecycleRepository().transition(session, intent.intent_id, "cancelled", reason=reason)
