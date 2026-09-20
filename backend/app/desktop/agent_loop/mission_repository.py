r"""本文件对外提供 MissionRevisionRepository。

输入为 AsyncSession、Loop identity、revision 与已验证的 LoopMissionContract；输出为持久化的
LoopMissionRevision 或指定 revision 的只读结果。具体工作流为按 Loop/revision 查询，追加时序列化强类型
contract 并依赖数据库唯一约束阻止重复 revision。示例：`await repository.append(session, ...)`。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.mission_contract import LoopMissionContract
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision


class MissionRevisionRepository:
    async def get(self, session: AsyncSession, loop_id: str, revision: int) -> LoopMissionRevision | None:
        return await session.scalar(
            select(LoopMissionRevision).where(
                LoopMissionRevision.loop_id == loop_id,
                LoopMissionRevision.revision == revision,
            )
        )

    async def history(self, session: AsyncSession, loop_id: str) -> tuple[LoopMissionRevision, ...]:
        rows = await session.scalars(
            select(LoopMissionRevision)
            .where(LoopMissionRevision.loop_id == loop_id)
            .order_by(LoopMissionRevision.revision)
        )
        return tuple(rows.all())

    async def append(
        self,
        session: AsyncSession,
        *,
        loop_id: str,
        revision: int,
        contract: LoopMissionContract,
        authored_by: str,
        legacy_goal_revision_id: str | None = None,
    ) -> LoopMissionRevision:
        row = LoopMissionRevision(
            mission_revision_id=uuid.uuid4().hex,
            loop_id=loop_id,
            revision=revision,
            outcome=contract.outcome,
            boundaries=contract.boundaries.model_dump(mode="json"),
            completion_checks=[item.model_dump(mode="json") for item in contract.completion_checks],
            legacy_goal_revision_id=legacy_goal_revision_id,
            authored_by=authored_by,
        )
        session.add(row)
        await session.flush()
        return row
