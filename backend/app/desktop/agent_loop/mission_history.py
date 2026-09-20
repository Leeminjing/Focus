r"""本文件对外提供 MissionHistoryQueryService。

输入为只读 AsyncSession 与 Loop id；输出为恰好一个 active revision 的结构化 Mission/旧合同历史。
具体工作流为读取结构化 revisions，排除其已链接的兼容 Goal 行，再把未链接旧 Goal 以
`legacy_goal_contract` 原样返回，不把 Task Contract 猜测为结构化边界。示例：`await service.read(session, loop_id)`。
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.models import AgentLoop, LoopGoalRevision


class MissionHistoryQueryService:
    async def read(self, session: AsyncSession, loop_id: str) -> dict:
        loop = await session.get(AgentLoop, loop_id)
        if loop is None:
            raise HTTPException(404, "Agent Loop 不存在")
        missions = tuple((await session.scalars(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == loop_id).order_by(LoopMissionRevision.revision))).all())
        goals = tuple((await session.scalars(select(LoopGoalRevision).where(LoopGoalRevision.loop_id == loop_id).order_by(LoopGoalRevision.revision))).all())
        linked = {row.legacy_goal_revision_id for row in missions if row.legacy_goal_revision_id}
        revisions = [
            {
                "kind": "structured_mission",
                "revision": row.revision,
                "active": row.revision == loop.goal_revision,
                "mission_revision_id": row.mission_revision_id,
                "outcome": row.outcome,
                "boundaries": row.boundaries,
                "completion_checks": row.completion_checks,
                "authored_by": row.authored_by,
                "created_at": row.created_at.isoformat(),
            }
            for row in missions
        ]
        revisions.extend(
            {
                "kind": "legacy_goal_contract",
                "revision": row.revision,
                "active": row.revision == loop.goal_revision and not any(item.revision == row.revision for item in missions),
                "goal_revision_id": row.goal_revision_id,
                "goal": row.goal,
                "task_contract": row.task_contract,
                "acceptance_criteria": row.acceptance_criteria,
                "authored_by": row.authored_by,
                "created_at": row.created_at.isoformat(),
            }
            for row in goals
            if row.goal_revision_id not in linked
        )
        revisions.sort(key=lambda item: (item["revision"], item["kind"]))
        return {"loop_id": loop_id, "active_revision": loop.goal_revision, "revisions": revisions}
