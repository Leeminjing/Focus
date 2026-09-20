r"""本文件对外提供 MissionAuthorityGuard 与 MissionAuthorityViolation。

输入为数据库 session、当前 Loop/round、Patrol intent 与结构化 Mission revision；输出为通过或带可定位
boundary identity 的确定性拒绝。具体工作流为核对 Loop、round、intent 指向同一 Mission revision，再将
规范 action name 禁止项和 `context:<id>` 范围应用到每个动作；普通自然语言边界保持可见但不被猜测解释。
示例：`await guard.validate(session, loop, round_row, intent)`。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.mission_contract import ExecutionBoundaries
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.models import AgentLoop, LoopRound
from backend.app.desktop.agent_loop.schemas import PatrolDecisionIntent


class MissionAuthorityViolation(RuntimeError):
    pass


class MissionAuthorityGuard:
    async def validate(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        round_row: LoopRound,
        intent: PatrolDecisionIntent,
    ) -> None:
        if not (loop.goal_revision == round_row.goal_revision == intent.goal_revision):
            raise MissionAuthorityViolation("mission_revision_changed")
        mission = await session.scalar(
            select(LoopMissionRevision).where(
                LoopMissionRevision.loop_id == loop.loop_id,
                LoopMissionRevision.revision == loop.goal_revision,
            )
        )
        if mission is None:
            return
        boundaries = ExecutionBoundaries.model_validate(mission.boundaries)
        self._validate_actions(boundaries, intent)

    @staticmethod
    def _validate_actions(boundaries: ExecutionBoundaries, intent: PatrolDecisionIntent) -> None:
        blocked = set(boundaries.blocked_action_types())
        scoped_contexts = set(boundaries.scoped_context_ids())
        for action in intent.actions:
            if action.action in blocked:
                raise MissionAuthorityViolation(f"boundary:prohibited_actions:{action.action}")
            context_id = getattr(action, "context_id", None)
            if scoped_contexts and context_id is not None and context_id not in scoped_contexts:
                raise MissionAuthorityViolation(f"boundary:in_scope:context:{context_id}")
