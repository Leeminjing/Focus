r"""本文件对外提供 MissionRevisionService。

输入为数据库 session、Loop/revision identity、强类型 Mission contract 与明确作者；输出为只允许用户创建的
Mission revision。具体工作流为拒绝隐式或非用户修订，再委托 Repository 追加不可变记录，事务提交仍由调用方
拥有。示例：`await service.record(session, loop_id="l1", revision=2, contract=mission, authored_by="user")`。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.mission_contract import LoopMissionContract
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_repository import MissionRevisionRepository


class MissionRevisionService:
    def __init__(self, repository: MissionRevisionRepository | None = None) -> None:
        self._repository = repository or MissionRevisionRepository()

    async def record(
        self,
        session: AsyncSession,
        *,
        loop_id: str,
        revision: int,
        contract: LoopMissionContract,
        authored_by: str,
        legacy_goal_revision_id: str | None = None,
    ) -> LoopMissionRevision:
        self._require_user_author(authored_by)
        return await self._repository.append(
            session,
            loop_id=loop_id,
            revision=revision,
            contract=contract,
            authored_by=authored_by,
            legacy_goal_revision_id=legacy_goal_revision_id,
        )

    @staticmethod
    def _require_user_author(authored_by: str) -> None:
        if authored_by != "user":
            raise PermissionError("Mission revision 必须由用户显式确认")
