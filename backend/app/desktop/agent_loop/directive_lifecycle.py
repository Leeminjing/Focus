r"""本文件对外提供 DirectiveLifecycleRepository 与 DirectiveTransitionRejected。

输入为同事务内已锁定的 LoopDirective、目标 lifecycle state、可选 Run/原因/causation；输出为递增 revision、
不可变 transition 与规范 journal event。具体工作流为 register 记录 proposed，transition 验证 authorized、delivery、
Run 与 terminal 单向状态，再把 current row、history、event 原子提交；当前尝试身份只在交付与启动类转换上记录，
终态转换即使携带外来 Run 也不改写它（该次转换携带的 Run 仍写入不可变 history 供审计）。
示例：`await repository.authorize(session, directive)`。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.models import LoopDirective, LoopDirectiveTransition


class DirectiveTransitionRejected(ValueError):
    pass


class DirectiveLifecycleRepository:
    _TERMINAL = frozenset({"rejected", "delivery_failed", "settled", "failed", "cancelled"})
    _RUN_BINDING_STATES = frozenset({"delivered", "run_started"})
    _EDGES = {
        "proposed": frozenset({"authorized", "rejected", "cancelled"}),
        "authorized": frozenset({"delivering", "cancelled"}),
        "delivering": frozenset({"authorized", "delivered", "delivery_failed", "cancelled"}),
        "delivered": frozenset({"run_started", "delivery_failed", "cancelled"}),
        "run_started": frozenset({"authorized", "settled", "failed", "cancelled"}),
    }

    def __init__(self) -> None:
        self._journal = LoopEventJournal()

    async def register(self, session: AsyncSession, directive: LoopDirective, *, causation_event_id: str | None = None) -> LoopDirective:
        await session.flush()
        existing = await session.scalar(
            select(LoopDirectiveTransition).where(
                LoopDirectiveTransition.directive_id == directive.directive_id,
                LoopDirectiveTransition.revision == 1,
            )
        )
        if existing is not None:
            return directive
        directive.revision = 1
        directive.lifecycle_state = "proposed"
        directive.causation_event_id = causation_event_id
        await self._record(session, directive, "proposed", "proposed", None, None, causation_event_id)
        return directive

    async def transition(
        self,
        session: AsyncSession,
        directive_id: str,
        target: str,
        *,
        reason: str | None = None,
        run_id: str | None = None,
        caused_by_event_id: str | None = None,
    ) -> LoopDirective:
        directive = await session.get(LoopDirective, directive_id, with_for_update=True)
        if directive is None:
            raise LookupError("Directive 不存在")
        current = directive.lifecycle_state
        if current in self._TERMINAL or target not in self._EDGES.get(current, frozenset()):
            raise DirectiveTransitionRejected(f"非法 Directive transition: {current} -> {target}")
        directive.revision += 1
        directive.lifecycle_state = target
        if run_id is not None and target in self._RUN_BINDING_STATES:
            directive.launched_run_id = run_id
        if target in self._TERMINAL:
            directive.terminal_reason = reason
        await self._record(session, directive, current, target, reason, run_id, caused_by_event_id)
        return directive

    async def history(self, session: AsyncSession, directive_id: str) -> tuple[LoopDirectiveTransition, ...]:
        return tuple(
            (
                await session.scalars(
                    select(LoopDirectiveTransition)
                    .where(LoopDirectiveTransition.directive_id == directive_id)
                    .order_by(LoopDirectiveTransition.revision)
                )
            ).all()
        )

    async def cancel_active(
        self,
        session: AsyncSession,
        loop_id: str,
        reason: str,
        *,
        round_id: str | None = None,
    ) -> int:
        query = (
            select(LoopDirective)
            .where(
                LoopDirective.loop_id == loop_id,
                LoopDirective.status.in_(("created", "launching")),
            )
            .with_for_update()
        )
        if round_id is not None:
            query = query.where(LoopDirective.round_id == round_id)
        directives = tuple((await session.scalars(query)).all())
        cancelled = 0
        for directive in directives:
            directive.status = "cancelled"
            directive.queued_reason = reason[:1000]
            if directive.lifecycle_state not in self._TERMINAL:
                await self.transition(session, directive.directive_id, "cancelled", reason=reason)
            cancelled += 1
        return cancelled

    async def _record(
        self,
        session: AsyncSession,
        directive: LoopDirective,
        previous: str,
        target: str,
        reason: str | None,
        run_id: str | None,
        caused_by_event_id: str | None,
    ) -> None:
        session.add(
            LoopDirectiveTransition(
                transition_id=uuid.uuid4().hex,
                directive_id=directive.directive_id,
                loop_id=directive.loop_id,
                round_id=directive.round_id,
                revision=directive.revision,
                from_state=previous,
                to_state=target,
                reason=reason,
                run_id=run_id,
                caused_by_event_id=caused_by_event_id,
            )
        )
        await self._journal.append(
            session,
            directive.loop_id,
            CanonicalEventDraft(
                kind=f"directive.{target}",
                entity_type="directive",
                entity_id=directive.directive_id,
                entity_revision=directive.revision,
                correlation_id=directive.correlation_id,
                causation_id=caused_by_event_id or directive.causation_event_id,
                payload={
                    "directive_id": directive.directive_id,
                    "round_id": directive.round_id,
                    "decision_id": directive.decision_id,
                    "origin": directive.origin_kind,
                    "actor_id": directive.actor_id,
                    "target_context_id": directive.target_context_id,
                    "state": target,
                    "run_id": run_id,
                    "reason": reason,
                },
                idempotency_key=f"directive:{directive.directive_id}:revision:{directive.revision}",
            ),
        )
