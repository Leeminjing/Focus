r"""本文件对外提供 PortfolioPublicationEventRecorder 与 ContextPublicationEventRecorder。

输入为已在同一事务确认发布的 Portfolio、Loop、round、decision、Directive、Context 与 Membership identity；输出为
`portfolio.published` 或 `context.created` 规范事件。具体工作流为将提交结果转换为安全、可重放且幂等的 canonical envelope，
由调用方与权威指针在同一事务提交。示例：`await recorder.record(session, loop_id="l", portfolio_id="p", generation=2, ...)`。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.models import LoopContextMembership
from backend.app.desktop.models import DesktopThread


class PortfolioPublicationEventRecorder:
    def __init__(self) -> None:
        self._journal = LoopEventJournal()

    async def record(
        self,
        session: AsyncSession,
        *,
        loop_id: str,
        portfolio_id: str,
        generation: int,
        round_id: str,
        decision_id: str,
        directive_ids: tuple[str, ...] = (),
    ) -> None:
        await self._journal.append(
            session,
            loop_id,
            CanonicalEventDraft(
                kind="portfolio.published",
                entity_type="portfolio",
                entity_id=portfolio_id,
                entity_revision=max(1, generation),
                correlation_id=decision_id,
                payload={
                    "portfolio_revision_id": portfolio_id,
                    "generation": generation,
                    "status": "published",
                    "round_id": round_id,
                    "decision_id": decision_id,
                    "directive_ids": list(directive_ids),
                },
                idempotency_key=f"portfolio:{portfolio_id}:published",
            ),
        )


class ContextPublicationEventRecorder:
    def __init__(self) -> None:
        self._journal = LoopEventJournal()

    async def record(
        self,
        session: AsyncSession,
        *,
        loop_id: str,
        decision_id: str,
        context: DesktopThread,
        membership: LoopContextMembership,
    ) -> None:
        await self._journal.append(
            session,
            loop_id,
            CanonicalEventDraft(
                kind="context.created",
                entity_type="context",
                entity_id=context.task_id,
                entity_revision=1,
                correlation_id=decision_id,
                payload={
                    "title": context.title,
                    "current_revision_id": context.current_revision_id,
                    "membership_id": membership.membership_id,
                    "lane_id": membership.lane_id,
                    "role": membership.role,
                    "status": membership.status,
                    "required_barrier": membership.required_barrier,
                },
                idempotency_key=f"context:{context.task_id}:membership:{membership.membership_id}:created",
            ),
        )
