"""采用意图提交至完成之间预留两个 slot；未知磁盘状态保留预留直至显式解决。调用方先持有 slot 行锁。"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.workspaces.coordination.models import WorkspaceAdoption


async def reserved_adoption(session: AsyncSession, *slot_ids: str) -> str | None:
    return await session.scalar(select(WorkspaceAdoption.adoption_id).where(
        or_(WorkspaceAdoption.source_slot_id.in_(slot_ids), WorkspaceAdoption.target_slot_id.in_(slot_ids)),
        WorkspaceAdoption.status.in_(("adopting", "conflict", "waiting_user")),
        WorkspaceAdoption.application_intent != {},
    ).limit(1))
