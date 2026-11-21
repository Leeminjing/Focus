r"""本文件对外提供 ContextConversationQueryService 的完整会话分页读取端口。

输入为 Loop/Context id、可选分页一致性状态标识、反向游标与页大小；输出为当前内容的有界消息页、
总数、下一游标、内容 hash、来源 Context 和独立 provenance。具体工作流为校验 Loop membership 后始终读取 current；
revision_id 只校验同次分页的状态，内容变化返回 409 要求显式刷新，不读取其他 owner 固定的旧输入。
内部 Observation 的精确快照读取独立保留；正文不注入来源标签。
示例：`await service.read(session, loop_id, context_id, revision_id=None, before=None, limit=40)`。
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.contexts.evolution import (
    ContextRevisionNotFound,
    ContextRevisionReader,
    ContextRevisionRepository,
)
from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop,
    LoopContextMembership,
    MessageProvenance,
)
from backend.app.desktop.patrol.agent_loop.queries.file_link_sources import (
    ConversationFileSources,
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
            revision = await self._repository.current(session, context_id)
        except ContextRevisionNotFound as exc:
            raise HTTPException(404, "Context 当前内容不存在") from exc
        if revision is None or revision.ref.context_id != context_id:
            raise HTTPException(404, "Context 当前内容不存在")
        if revision_id is not None and revision_id != revision.ref.revision_id:
            raise HTTPException(409, "Context 内容已更新，请刷新当前内容后继续阅读")
        page = await self._reader.read_message_page(
            revision,
            before=before,
            limit=limit,
        )
        message_ids = tuple(
            str(message["id"])
            for message in page.messages
            if message.get("id") is not None
        )
        provenance = await self._provenance(
            session,
            revision.ref.revision_id,
            message_ids,
        )
        file_sources = ConversationFileSources(session, self._repository, self._reader, loop.workspace_id)
        file_contexts = {}
        for message in page.messages:
            if (message.get("role") or message.get("type")) in {"ai", "assistant"}:
                file_contexts[str(message.get("id"))] = await file_sources.for_message(revision, message)
        return {
            "loop_id": loop_id,
            "context_id": context_id,
            "revision": revision.ref.model_dump(mode="json"),
            "content_hash": revision.content_hash,
            "sources": [{"context_id": edge.source.context_id} for edge in revision.sources],
            "projection_status": revision.projection_status.value,
            "total": page.total,
            "range": {"start": page.start, "end": page.end},
            "next_before": page.start if page.start > 0 else None,
            "has_more": page.start > 0,
            "messages": [
                {
                    "index": page.start + offset,
                    "message": message,
                    "provenance": provenance.get(str(message.get("id"))),
                    "file_context": file_contexts.get(str(message.get("id"))),
                }
                for offset, message in enumerate(page.messages)
            ],
        }

    @staticmethod
    async def _provenance(
        session: AsyncSession,
        revision_id: str,
        message_ids: tuple[str, ...],
    ) -> dict[str, dict]:
        if not message_ids:
            return {}
        rows = list(
            (
                await session.scalars(
                    select(MessageProvenance).where(
                        MessageProvenance.context_revision_id == revision_id,
                        MessageProvenance.message_id.in_(message_ids),
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
