r"""本文件对外提供 InterventionLifecycleRepository 与 InterventionTransitionRejected。

输入为真实 user intent、目标 delivery state、可选 Run 与失败原因；输出为递增 revision、不可变 transition 与规范事件。
具体工作流为 register 记录 submitted，随后按 user→Context 的 delivery/run 链或 user→Patrol 的 observed/addressed 链推进，
两条路径均保留 origin=user 且不修改 Mission。示例：`await repository.transition(session, intent_id, "accepted")`。
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.models import LoopInterventionTransition, LoopUserIntent


class InterventionTransitionRejected(ValueError):
    pass


class InterventionLifecycleRepository:
    _TERMINAL = frozenset({"addressed", "rejected", "delivery_failed", "settled", "failed", "cancelled"})
    _EDGES = {
        "submitted": frozenset({"accepted", "rejected", "cancelled"}),
        "accepted": frozenset({"observed", "delivered", "rejected", "cancelled"}),
        "observed": frozenset({"addressed", "rejected", "cancelled"}),
        "delivered": frozenset({"run_started", "delivery_failed", "cancelled"}),
        "run_started": frozenset({"settled", "failed", "cancelled"}),
    }

    def __init__(self) -> None:
        self._journal = LoopEventJournal()

    async def register(self, session: AsyncSession, intent: LoopUserIntent) -> LoopUserIntent:
        await session.flush()
        if intent.revision:
            return intent
        intent.revision = 1
        intent.delivery_state = "submitted"
        await self._record(session, intent, "submitted", "submitted", None, None)
        return intent

    async def transition(
        self,
        session: AsyncSession,
        intent_id: str,
        target: str,
        *,
        run_id: str | None = None,
        reason: str | None = None,
    ) -> LoopUserIntent:
        intent = await session.get(LoopUserIntent, intent_id, with_for_update=True)
        if intent is None:
            raise LookupError("User intervention 不存在")
        current = intent.delivery_state
        if current in self._TERMINAL or target not in self._EDGES.get(current, frozenset()):
            raise InterventionTransitionRejected(f"非法 intervention transition: {current} -> {target}")
        intent.revision += 1
        intent.delivery_state = target
        if run_id is not None:
            intent.resulting_run_id = run_id
        if target in self._TERMINAL:
            intent.terminal_reason = reason
            intent.addressed_at = datetime.now(UTC)
        await self._record(session, intent, current, target, run_id, reason)
        return intent

    async def history(self, session: AsyncSession, intent_id: str) -> tuple[LoopInterventionTransition, ...]:
        return tuple(
            (
                await session.scalars(
                    select(LoopInterventionTransition)
                    .where(LoopInterventionTransition.intent_id == intent_id)
                    .order_by(LoopInterventionTransition.revision)
                )
            ).all()
        )

    async def _record(self, session, intent, previous, target, run_id, reason) -> None:
        session.add(
            LoopInterventionTransition(
                transition_id=uuid.uuid4().hex,
                intent_id=intent.intent_id,
                loop_id=intent.loop_id,
                revision=intent.revision,
                from_state=previous,
                to_state=target,
                run_id=run_id,
                reason=reason,
            )
        )
        await self._journal.append(
            session,
            intent.loop_id,
            CanonicalEventDraft(
                kind=f"intervention.{target}",
                entity_type="intervention",
                entity_id=intent.intent_id,
                entity_revision=intent.revision,
                correlation_id=intent.correlation_id,
                payload={
                    "intent_id": intent.intent_id,
                    "origin": intent.origin_kind,
                    "scope": intent.scope,
                    "target_context_id": intent.target_context_id,
                    "state": target,
                    "run_id": run_id,
                    "reason": reason,
                },
                idempotency_key=f"intervention:{intent.intent_id}:revision:{intent.revision}",
            ),
        )
