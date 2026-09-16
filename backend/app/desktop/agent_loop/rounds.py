r"""本文件对外提供 create_observation_round，统一创建可由 Coordinator 认领的观察轮。

输入为已锁定的 AgentLoop、可选前一轮与确定性 fallback frontier hash；输出为绑定当前 authority、goal
和权威 workspace revision 的新 LoopRound。具体工作流为读取权威 Workspace Slot、分配单调轮号并
继承稳定 frontier；本函数只构造对象，不提交事务。示例：`round_row = await create_observation_round(...)`。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.models import AgentLoop, LoopRound
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


async def create_observation_round(
    session: AsyncSession,
    loop: AgentLoop,
    prior: LoopRound | None,
    fallback_frontier_hash: str,
) -> LoopRound:
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
    return LoopRound(
        round_id=uuid.uuid4().hex,
        loop_id=loop.loop_id,
        number=number,
        authority_revision=loop.authority_revision,
        goal_revision=loop.goal_revision,
        frontier_hash=prior.frontier_hash if prior else fallback_frontier_hash,
        workspace_revision=slot.revision if slot else 1,
    )
