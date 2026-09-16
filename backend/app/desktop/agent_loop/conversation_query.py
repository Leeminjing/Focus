r"""本文件对外提供 ContextConversationQueryService 的完整会话分页读取端口。

输入为 Loop/Context id、可选 revision、反向游标与页大小；输出为按原始顺序排列的消息页、总数、
下一游标和独立 provenance。具体工作流为校验 Loop membership，精确读取不可变 revision display
投影，再从尾部向前分页并按 message id 关联审计来源；正文不注入来源标签。
示例：`await service.read(session, loop_id, context_id, before=None, limit=40)`。
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopContextMembership,
    MessageProvenance,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionNotFound,
    ContextRevisionReader,
    ContextRevisionRepository,
)


class ContextConversationQueryService:
    def __init__(self, checkpointer) -> None:
        self._repository = ContextRevisionRepository()
        self._reader = ContextRevisionReader(self._repository, checkpointer)

    async def read(
        self,
        session: AsyncSession,
        loop_id: str,
        context_id: str,
        *,
        revision_id: str | None,
        before: int | None,
        limit: int,
    ) -> dict:
        loop = await session.get(AgentLoop, loop_id)
        if loop is None:
            raise HTTPException(404, "Agent Loop 不存在")
        membership = await session.scalar(
            select(LoopContextMembership).where(
                LoopContextMembership.loop_id == loop_id,
                LoopContextMembership.context_id == context_id,
            )
        )
        if membership is None:
            raise HTTPException(404, "Context 不属于当前 Loop")
        try:
            revision = (
                await self._repository.get_by_id(session, revision_id)
                if revision_id
                else await self._repository.current(session, context_id)
            )
        except ContextRevisionNotFound as exc:
            raise HTTPException(404, "Context revision 不存在") from exc
        if revision is None or revision.ref.context_id != context_id:
            raise HTTPException(404, "Context revision 不属于目标 Context")
        view = await self._reader.read(session, revision.ref, "display")
        messages = list(view.messages)
        total = len(messages)
        end = total if before is None else min(max(before, 0), total)
        start = max(0, end - limit)
        page = messages[start:end]
        provenance = await self._provenance(session, revision.ref.revision_id)
        return {
            "loop_id": loop_id,
            "context_id": context_id,
            "revision": revision.ref.model_dump(mode="json"),
            "projection_status": revision.projection_status.value,
            "total": total,
            "range": {"start": start, "end": end},
            "next_before": start if start > 0 else None,
            "has_more": start > 0,
            "messages": [
                {
                    "index": start + offset,
                    "message": message,
                    "provenance": provenance.get(str(message.get("id"))),
                }
                for offset, message in enumerate(page)
            ],
        }

    @staticmethod
    async def _provenance(session: AsyncSession, revision_id: str) -> dict[str, dict]:
        rows = list(
            (
                await session.scalars(
                    select(MessageProvenance).where(
                        MessageProvenance.context_revision_id == revision_id
                    )
                )
            ).all()
        )
        return {
            row.message_id: {
                "provenance_id": row.provenance_id,
                "source_kind": row.source_kind,
                "actor_id": row.actor_id,
                "directive_id": row.directive_id,
                "audit": row.audit,
            }
            for row in rows
        }
