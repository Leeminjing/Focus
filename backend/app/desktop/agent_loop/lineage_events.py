r"""本文件对外提供 ContextLineageEventRecorder。

对外提供:
    ContextLineageEventRecorder.record_loop_members — 把 Loop 当前全部成员的跨 Context 派生边登记为 journal 事实

输入为与调用方事务共享的 AsyncSession 和 Loop id。输出为该 Loop 每个成员 Context 的每条跨 Context 来源边对应的
`context.lineage.derived` canonical event，同一 Context 内的 revision 链不产生事件。

具体工作流为读取该 Loop 的 Context membership 与各自当前 revision，交给 ContextLineageResolver 解析成员之间的
派生边，再以「来源 Context:目标 Context」为实体、目标 revision 代次为 revision 写入事件；幂等键绑定目标 revision，
重复登记不会产生第二个事件。登记发生在 Loop 纳成员或发布 portfolio 的同一事务内，因此派生边与 Loop 的拓扑变化
共用同一次提交边界。

示例:
    await ContextLineageEventRecorder().record_loop_members(session, loop_id=loop.loop_id)
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.models import LoopContextMembership
from backend.app.desktop.context_evolution.lineage import ContextLineageResolver
from backend.app.desktop.models import DesktopThread


class ContextLineageEventRecorder:
    KIND = "context.lineage.derived"
    ENTITY_TYPE = "context_lineage"

    def __init__(
        self,
        journal: LoopEventJournal | None = None,
        lineage: ContextLineageResolver | None = None,
    ) -> None:
        self._journal = journal or LoopEventJournal()
        self._lineage = lineage or ContextLineageResolver()

    async def record_loop_members(self, session: AsyncSession, *, loop_id: str) -> int:
        current = await self._member_revisions(session, loop_id)
        if len(current) < 2:
            return 0
        recorded = 0
        for edge in await self._lineage.resolve(session, current):
            await self._journal.append(
                session,
                loop_id,
                CanonicalEventDraft(
                    kind=self.KIND,
                    entity_type=self.ENTITY_TYPE,
                    entity_id=edge.entity_id,
                    entity_revision=edge.target_generation,
                    payload=edge.payload(),
                    idempotency_key=f"context-lineage:{edge.entity_id}:{edge.target_revision_id}",
                ),
            )
            recorded += 1
        return recorded

    @staticmethod
    async def _member_revisions(session: AsyncSession, loop_id: str) -> dict[str, str]:
        rows = (
            await session.execute(
                select(DesktopThread.task_id, DesktopThread.current_revision_id)
                .join(
                    LoopContextMembership,
                    LoopContextMembership.context_id == DesktopThread.task_id,
                )
                .where(LoopContextMembership.loop_id == loop_id)
            )
        ).all()
        return {task_id: revision_id for task_id, revision_id in rows if revision_id}
