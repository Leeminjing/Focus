r"""本文件对外提供 LoopInterventionService，持久化用户给 Portfolio Patrol 的控制意见。

输入为 Loop id、Context/Portfolio 作用域、可选目标 Context 与意见正文；输出为可审计 intent 和
是否已建立新观察轮。具体工作流为校验有效 delegation 与 membership，废弃尚未提交的 Patrol 判断，
在无活动 Run 时建立新的 observation round；有活动 Run 时让意图等待稳定边界，不中断执行 Agent。
示例：`await service.submit(loop_id, request)`。
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopContextMembership,
    LoopDecision,
    LoopDelegationGrant,
    LoopDirective,
    LoopEventOutbox,
    LoopRound,
    LoopUserIntent,
)
from backend.app.desktop.agent_loop.schemas import LoopInterventionRequest
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


class LoopInterventionService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._directives = DirectiveLifecycleRepository()
        self._lifecycle = InterventionLifecycleRepository()

    async def submit(self, loop_id: str, request: LoopInterventionRequest) -> dict:
        content = request.content.strip()
        if not content:
            raise HTTPException(422, "用户意见不能为空")
        scope = request.scope()
        if scope == "context" and not request.context_id:
            raise HTTPException(422, "Context 意见必须指定 context_id")
        if scope == "portfolio" and request.context_id is not None:
            raise HTTPException(422, "Portfolio 意见不能指定 context_id")
        async with self._sessions.begin() as session:
            loop = await session.scalar(
                select(AgentLoop).where(AgentLoop.loop_id == loop_id).with_for_update()
            )
            if loop is None:
                raise HTTPException(404, "Agent Loop 不存在")
            if loop.status not in {"running", "paused", "waiting_user"}:
                raise HTTPException(409, f"Loop 状态 {loop.status} 不接受新意见")
            grant = await session.scalar(
                select(LoopDelegationGrant).where(
                    LoopDelegationGrant.loop_id == loop_id,
                    LoopDelegationGrant.status == "active",
                )
            )
            if grant is None:
                raise HTTPException(409, "Patrol delegation 已撤销")
            if request.context_id:
                membership = await session.scalar(
                    select(LoopContextMembership).where(
                        LoopContextMembership.loop_id == loop_id,
                        LoopContextMembership.context_id == request.context_id,
                        LoopContextMembership.status.in_(["active", "paused"]),
                    )
                )
                if membership is None:
                    raise HTTPException(422, "目标 Context 不属于当前 Loop")
            intent_id = uuid.uuid4().hex
            intent = LoopUserIntent(
                intent_id=intent_id,
                loop_id=loop_id,
                scope=scope,
                target_context_id=request.context_id,
                content=content,
                goal_revision=loop.goal_revision,
                authority_revision=loop.authority_revision,
                correlation_id=intent_id,
            )
            session.add(intent)
            await self._lifecycle.register(session, intent)
            await self._lifecycle.transition(session, intent.intent_id, "accepted")
            await self._supersede_uncommitted(session, loop_id)
            loop.revision += 1
            loop.status = "running"
            loop.waiting_reason = None
            active_runs = int(
                await session.scalar(
                    select(func.count()).select_from(DesktopRun).where(
                        DesktopRun.loop_id == loop_id,
                        DesktopRun.status.in_(["pending", "running"]),
                    )
                )
                or 0
            )
            round_row = None
            if active_runs:
                loop.health = "waiting_runs"
            else:
                round_row = await self._new_round(session, loop)
                loop.current_round_id = round_row.round_id
                loop.health = "observing"
                session.add(round_row)
            await self._append_event(
                session,
                loop,
                "UserPatrolIntentSubmitted",
                {
                    "intent_id": intent.intent_id,
                    "scope": scope,
                    "context_id": request.context_id,
                    "round_id": round_row.round_id if round_row else None,
                    "waits_for_active_runs": bool(active_runs),
                },
            )
            return {
                "intent_id": intent.intent_id,
                "scope": scope,
                "context_id": request.context_id,
                "status": intent.status,
                "round_id": round_row.round_id if round_row else None,
                "waits_for_active_runs": bool(active_runs),
            }

    async def _supersede_uncommitted(self, session: AsyncSession, loop_id: str) -> None:
        await session.execute(
            update(LoopRound)
            .where(
                LoopRound.loop_id == loop_id,
                LoopRound.status.in_(["observed", "curated", "ready", "waiting_workers", "publishing", "adopting"]),
            )
            .values(status="superseded")
        )
        await session.execute(
            update(LoopDecision)
            .where(
                LoopDecision.loop_id == loop_id,
                LoopDecision.status.in_(["pending", "publishing", "adopting"]),
            )
            .values(status="superseded")
        )
        await self._directives.cancel_active(session, loop_id, "user_intervention")

    @staticmethod
    async def _new_round(session: AsyncSession, loop: AgentLoop) -> LoopRound:
        prior = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
        slot = await session.scalar(
            select(WorkspaceSlot).where(
                WorkspaceSlot.workspace_id == loop.workspace_id,
                WorkspaceSlot.kind == "authoritative",
                WorkspaceSlot.lifecycle != "deleted",
            )
        )
        number = int(
            await session.scalar(select(func.max(LoopRound.number)).where(LoopRound.loop_id == loop.loop_id))
            or 0
        ) + 1
        frontier_hash = prior.frontier_hash if prior else hashlib.sha256(loop.initial_context_id.encode()).hexdigest()
        return LoopRound(
            round_id=uuid.uuid4().hex,
            loop_id=loop.loop_id,
            number=number,
            authority_revision=loop.authority_revision,
            goal_revision=loop.goal_revision,
            frontier_hash=frontier_hash,
            workspace_revision=slot.revision if slot else 1,
        )

    @staticmethod
    async def _append_event(
        session: AsyncSession,
        loop: AgentLoop,
        event_type: str,
        payload: dict,
    ) -> None:
        sequence = int(
            await session.scalar(
                select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(
                    LoopEventOutbox.loop_id == loop.loop_id
                )
            )
            or 0
        ) + 1
        session.add(
            LoopEventOutbox(
                event_id=uuid.uuid4().hex,
                loop_id=loop.loop_id,
                sequence=sequence,
                event_type=event_type,
                payload=payload,
                idempotency_key=f"intent:{payload['intent_id']}",
            )
        )
