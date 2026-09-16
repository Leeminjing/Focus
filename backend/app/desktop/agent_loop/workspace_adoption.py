r"""本文件对外提供 LoopWorkspaceAdoptionService。

输入为 Kernel 已授权的单个 adopt_workspace_result decision；输出为 adopted 或 conflict 的 Kernel 结果。
具体工作流为重验当前用户 delegation、来源 slot 所有权和目标 workspace revision，调用可恢复的
WorkspaceAdopter 应用隔离 Git 结果，再提交 action/decision/anchor、推进新观察轮并写持久事件；Worker
和 Patrol 模型都不能直接改权威文件。示例：`result = await service.adopt(decision_id)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopAction,
    LoopDecision,
    LoopDelegationGrant,
    LoopEventOutbox,
    LoopRound,
)
from backend.app.desktop.agent_loop.schemas import AdoptWorkspaceResultAction
from backend.app.desktop.workspace_coordination import (
    GitWorkspaceResultApplier,
    RunExecutionAnchor,
    WorkspaceAdopter,
    WorkspaceAdoptionRequest,
    WorkspaceSlot,
)


class LoopWorkspaceAdoptionService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._adopter = WorkspaceAdopter(sessions)
        self._applier = GitWorkspaceResultApplier()

    async def adopt(self, decision_id: str):
        async with self._sessions.begin() as session:
            request = await self._request(session, decision_id)
            adoption = await self._adopter.adopt(request, self._applier)
            return await self._finish(session, decision_id, adoption)

    async def _request(self, session: AsyncSession, decision_id: str) -> WorkspaceAdoptionRequest:
        decision = await session.get(LoopDecision, decision_id, with_for_update=True)
        if decision is None or decision.status != "adopting":
            raise RuntimeError("Workspace adoption decision 已失效")
        loop = await session.get(AgentLoop, decision.loop_id, with_for_update=True)
        round_row = await session.get(LoopRound, decision.round_id, with_for_update=True)
        action_row = await session.scalar(
            select(LoopAction).where(LoopAction.decision_id == decision_id).with_for_update()
        )
        action = AdoptWorkspaceResultAction.model_validate(action_row.payload) if action_row else None
        grant = await session.scalar(
            select(LoopDelegationGrant).where(
                LoopDelegationGrant.loop_id == decision.loop_id,
                LoopDelegationGrant.revision == loop.authority_revision if loop else -1,
                LoopDelegationGrant.status == "active",
            ).with_for_update()
        )
        source = await session.get(WorkspaceSlot, action.source_slot_id) if action else None
        target = await session.scalar(
            select(WorkspaceSlot).where(
                WorkspaceSlot.workspace_id == loop.workspace_id if loop else "",
                WorkspaceSlot.kind == "authoritative",
                WorkspaceSlot.lifecycle == "active",
            )
        )
        if loop is None or round_row is None or action_row is None or action is None or grant is None:
            raise RuntimeError("Workspace adoption authority identity 不完整")
        if loop.status != "running" or source is None or target is None:
            raise RuntimeError("Workspace adoption slot 或 Loop 状态已失效")
        if source.owner_loop_id != loop.loop_id or source.kind != "isolated":
            raise RuntimeError("Workspace adoption 来源不属于当前 Loop")
        if round_row.authority_revision != grant.revision or round_row.goal_revision != loop.goal_revision:
            raise RuntimeError("Workspace adoption authority/goal revision 已变化")
        return WorkspaceAdoptionRequest(
            adoption_id=action_row.action_id,
            source_slot_id=source.slot_id,
            target_slot_id=target.slot_id,
            source_revision=action.source_revision,
            expected_target_revision=round_row.workspace_revision,
            evidence={
                "decision_id": decision.decision_id,
                "round_id": round_row.round_id,
                "patrol_rationale": action.rationale,
            },
        )

    async def _finish(self, session: AsyncSession, decision_id: str, adoption):
        from backend.app.desktop.agent_loop.kernel import KernelCommitResult

        decision = await session.get(LoopDecision, decision_id, with_for_update=True)
        action = await session.scalar(
            select(LoopAction).where(LoopAction.decision_id == decision_id).with_for_update()
        )
        loop = await session.get(AgentLoop, decision.loop_id, with_for_update=True) if decision else None
        round_row = await session.get(LoopRound, decision.round_id, with_for_update=True) if decision else None
        if decision is None or action is None or loop is None or round_row is None:
            raise RuntimeError("Workspace adoption completion identity 不完整")
        if decision.status == "committed":
            return KernelCommitResult(decision_id, "committed", (action.action_id,), ())
        anchors = list(
            (
                await session.scalars(
                    select(RunExecutionAnchor).where(
                        RunExecutionAnchor.slot_id == adoption.source_slot_id,
                        RunExecutionAnchor.resulting_workspace_revision == adoption.source_revision,
                    ).with_for_update()
                )
            ).all()
        )
        if adoption.status != "adopted":
            for anchor in anchors:
                anchor.adoption_state = "conflict"
            action.status = "conflict"
            action.result = {"adoption_id": adoption.adoption_id, "conflict": adoption.conflict}
            decision.status = "rejected"
            decision.rejection = adoption.conflict
            round_row.status = "error"
            loop.status = "waiting_user"
            loop.health = "degraded"
            loop.waiting_reason = f"Workspace adoption 冲突: {adoption.conflict.get('reason', 'unknown')}"
            await self._event(session, loop.loop_id, "WorkspaceAdoptionConflict", action.result, f"adoption-conflict:{adoption.adoption_id}")
            return KernelCommitResult(decision_id, "rejected", (action.action_id,), (), loop.waiting_reason)
        for anchor in anchors:
            anchor.adoption_state = "adopted"
        action.status = "applied"
        action.result = {
            "adoption_id": adoption.adoption_id,
            "source_slot_id": adoption.source_slot_id,
            "target_slot_id": adoption.target_slot_id,
            "resulting_target_revision": adoption.resulting_target_revision,
        }
        decision.status = "committed"
        round_row.status = "settled"
        round_row.settled_at = datetime.now(UTC)
        number = int(
            await session.scalar(
                select(func.coalesce(func.max(LoopRound.number), 0)).where(
                    LoopRound.loop_id == loop.loop_id
                )
            )
            or 0
        ) + 1
        next_round = LoopRound(
            round_id=uuid.uuid4().hex,
            loop_id=loop.loop_id,
            number=number,
            authority_revision=loop.authority_revision,
            goal_revision=loop.goal_revision,
            frontier_hash=round_row.frontier_hash,
            workspace_revision=adoption.resulting_target_revision,
        )
        session.add(next_round)
        loop.current_round_id = next_round.round_id
        loop.health = "observing"
        loop.revision += 1
        await self._event(session, loop.loop_id, "WorkspaceResultAdopted", action.result, f"adoption:{adoption.adoption_id}")
        await self._event(session, loop.loop_id, "RoundObserved", {"round_id": next_round.round_id}, f"adoption-round:{adoption.adoption_id}")
        return KernelCommitResult(decision_id, "committed", (action.action_id,), ())

    @staticmethod
    async def _event(session, loop_id: str, event_type: str, payload: dict, key: str) -> None:
        sequence = int(
            await session.scalar(
                select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(
                    LoopEventOutbox.loop_id == loop_id
                )
            )
            or 0
        ) + 1
        session.add(
            LoopEventOutbox(
                event_id=uuid.uuid4().hex,
                loop_id=loop_id,
                sequence=sequence,
                event_type=event_type,
                payload=payload,
                idempotency_key=key,
            )
        )
