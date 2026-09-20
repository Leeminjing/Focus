r"""本文件对外提供 LoopPortfolioPublicationQueue。

输入为已由 Kernel 持久授权且状态为 publishing 的 LoopDecision、可选 Loop scope、发布端口、并发上限和最大尝试次数；输出为
独立于 Run event 消费的有界发布任务。具体工作流为按依赖锁定可发布 decision，记录稳定 attempt identity，
后台执行原子发布，成功后收口用户意图与 Patrol Session，失败则有界重排队或显式终结并保留 Session 因果。
示例：`await queue.drain()`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import AgentLoop, LoopAction, LoopDecision, LoopRound, LoopUserIntent
from backend.app.desktop.agent_loop.patrol_session_state import PatrolActivity, PatrolPhase
from backend.app.desktop.agent_loop.patrol_runtime import PatrolSessionLifecycle
from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.context_curation.portfolio_publisher import PortfolioSuperseded


class PortfolioPublicationPort(Protocol):
    async def publish(self, decision_id: str): ...


class LoopPortfolioPublicationQueue:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        publisher: PortfolioPublicationPort,
        *,
        concurrency: int = 2,
        max_attempts: int = 3,
    ) -> None:
        self._sessions = sessions
        self._publisher = publisher
        self._concurrency = max(1, concurrency)
        self._max_attempts = max(1, max_attempts)
        self._tasks: dict[str, asyncio.Task] = {}
        self._patrol_sessions = PatrolSessionLifecycle(sessions)
        self._interventions = InterventionLifecycleRepository()

    async def recover(self) -> int:
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(LoopDecision)
                .where(LoopDecision.status == "publishing_run")
                .values(status="publishing", queued_reason="process_restarted")
            )
            return int(result.rowcount or 0)

    async def drain(self, loop_id: str | None = None) -> int:
        self._reap()
        capacity = self._concurrency - len(self._tasks)
        if capacity <= 0:
            return 0
        decision_ids = await self._claim(capacity, loop_id)
        for decision_id in decision_ids:
            task = asyncio.create_task(self._run(decision_id), name=f"loop-portfolio-publication:{decision_id}")
            self._tasks[decision_id] = task
        return len(decision_ids)

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _claim(self, limit: int, loop_id: str | None = None) -> tuple[str, ...]:
        async with self._sessions.begin() as session:
            statement = (
                select(LoopDecision)
                .join(AgentLoop, AgentLoop.loop_id == LoopDecision.loop_id)
                .join(LoopRound, LoopRound.round_id == LoopDecision.round_id)
                .where(
                    LoopDecision.status == "publishing",
                    AgentLoop.status == "running",
                    LoopRound.status == "publishing",
                )
                .order_by(LoopDecision.created_at, LoopDecision.decision_id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
            if loop_id is not None:
                statement = statement.where(LoopDecision.loop_id == loop_id)
            rows = list(
                (
                    await session.scalars(statement)
                ).all()
            )
            for row in rows:
                row.status = "publishing_run"
                row.deferred_attempt += 1
                row.queued_reason = None
            return tuple(row.decision_id for row in rows)

    async def _run(self, decision_id: str) -> None:
        try:
            await self._publisher.publish(decision_id)
            await self._address_user_intents(decision_id)
            await self._settle_patrol(decision_id, PatrolPhase.COMPLETED, "Portfolio 已发布")
        except asyncio.CancelledError:
            await self._requeue(decision_id, "component_stopped")
            raise
        except PortfolioSuperseded as exc:
            await self._settle_failure(decision_id, str(exc), superseded=True)
            await self._settle_patrol(decision_id, PatrolPhase.SUPERSEDED, "Portfolio publication 已被更新状态取代", str(exc))
        except Exception as exc:
            terminal = await self._settle_failure(decision_id, str(exc), superseded=False)
            if terminal:
                await self._settle_patrol(decision_id, PatrolPhase.FAILED, "Portfolio publication 失败", str(exc))

    async def _address_user_intents(self, decision_id: str) -> None:
        async with self._sessions.begin() as session:
            round_id = await session.scalar(
                select(LoopDecision.round_id).where(LoopDecision.decision_id == decision_id)
            )
            if round_id is not None:
                intents = tuple(
                    (
                        await session.scalars(
                            select(LoopUserIntent)
                            .where(LoopUserIntent.observed_round_id == round_id, LoopUserIntent.status == "observed")
                            .with_for_update()
                        )
                    ).all()
                )
                for intent in intents:
                    intent.status = "addressed"
                    if intent.delivery_state == "observed":
                        await self._interventions.transition(session, intent.intent_id, "addressed")

    async def _requeue(self, decision_id: str, reason: str) -> None:
        async with self._sessions.begin() as session:
            decision = await session.get(LoopDecision, decision_id, with_for_update=True)
            if decision is not None and decision.status == "publishing_run":
                decision.status = "publishing"
                decision.queued_reason = reason[:1000]

    async def _settle_failure(self, decision_id: str, reason: str, *, superseded: bool) -> bool:
        async with self._sessions.begin() as session:
            decision = await session.get(LoopDecision, decision_id, with_for_update=True)
            if decision is None or decision.status != "publishing_run":
                return False
            loop = await session.get(AgentLoop, decision.loop_id, with_for_update=True)
            round_row = await session.get(LoopRound, decision.round_id, with_for_update=True)
            if not superseded and loop is not None and loop.status == "running" and decision.deferred_attempt < self._max_attempts:
                decision.status = "publishing"
                decision.queued_reason = f"retry:{decision.decision_id}:attempt:{decision.deferred_attempt + 1}:{reason[:500]}"
                return False
            status = "superseded" if superseded or loop is None or loop.status != "running" else "rejected"
            decision.status = status
            decision.rejection = {"reason": reason[:2000], "attempts": decision.deferred_attempt}
            decision.queued_reason = None
            actions = list(
                (
                    await session.scalars(
                        select(LoopAction).where(LoopAction.decision_id == decision_id).with_for_update()
                    )
                ).all()
            )
            for action in actions:
                if action.status == "authorized":
                    action.status = status
            if round_row is not None and round_row.status == "publishing":
                round_row.status = "superseded" if status == "superseded" else "error"
            if loop is not None and loop.status == "running" and status == "rejected":
                loop.status = "waiting_user"
                loop.health = "degraded"
                loop.waiting_reason = f"Portfolio publication 失败: {reason[:1000]}"
            return True

    async def _settle_patrol(
        self,
        decision_id: str,
        phase: PatrolPhase,
        summary: str,
        reason: str | None = None,
    ) -> None:
        async with self._sessions() as session:
            round_id = await session.scalar(select(LoopDecision.round_id).where(LoopDecision.decision_id == decision_id))
        if round_id is None:
            return
        patrol_session = await self._patrol_sessions.current(round_id)
        if patrol_session is None or patrol_session.phase in {
            PatrolPhase.COMPLETED,
            PatrolPhase.FAILED,
            PatrolPhase.INTERRUPTED,
            PatrolPhase.SUPERSEDED,
        }:
            return
        if patrol_session.phase == PatrolPhase.AUTHORIZING:
            patrol_session = await self._patrol_sessions.transition(
                patrol_session.session_id,
                PatrolPhase.PUBLISHING,
                PatrolActivity(summary="Portfolio publication 已开始"),
            )
        if patrol_session.phase != PatrolPhase.PUBLISHING:
            return
        await self._patrol_sessions.transition(
            patrol_session.session_id,
            phase,
            PatrolActivity(summary=summary),
            terminal_outcome={"status": phase.value, "decision_id": decision_id, "reason": reason},
        )

    def _reap(self) -> None:
        completed = tuple(decision_id for decision_id, task in self._tasks.items() if task.done())
        for decision_id in completed:
            task = self._tasks.pop(decision_id)
            if not task.cancelled():
                task.exception()
