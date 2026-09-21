r"""本文件对外提供 ContextExpansionRepository 与 ExpansionRepositoryRejected。

输入为 AsyncSession、ExpansionOpportunity、policy level、目标 lifecycle state、安全摘要和结果引用；输出为幂等持久化的
LoopContextExpansion 与追加 transition/journal 事件。具体工作流为 create 先按稳定 expansion identity 查重，transition
行锁当前记录并用状态机验证；活动覆盖只包含未决 expansion 或仍具 active Lane/Membership 的 dispatched expansion，随后同步
当前投影、历史和规范事件。示例：`row = await repository.create(session, opportunity, ...)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionBlockerCode, ExpansionOpportunity
from backend.app.desktop.agent_loop.context_expansion.lifecycle import ExpansionLifecycleStateMachine, ExpansionTransitionRejected
from backend.app.desktop.agent_loop.context_expansion.models import LoopContextExpansion, LoopContextExpansionTransition
from backend.app.desktop.agent_loop.models import LoopContextMembership
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.context_curation.models import CurationLane


class ExpansionRepositoryRejected(ValueError):
    pass


class ContextExpansionRepository:
    _TERMINAL = frozenset({"dispatched", "declined", "blocked", "failed", "superseded"})

    def __init__(self) -> None:
        self._machine = ExpansionLifecycleStateMachine()
        self._journal = LoopEventJournal()

    @classmethod
    def is_terminal(cls, state: str) -> bool:
        return state in cls._TERMINAL

    async def create(
        self,
        session: AsyncSession,
        opportunity: ExpansionOpportunity,
        *,
        policy_version: str,
        level: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> LoopContextExpansion:
        existing = await session.get(LoopContextExpansion, opportunity.opportunity_id, with_for_update=True)
        if existing is not None:
            return existing
        summary = f"发现 Context 派生机会：{opportunity.purpose}"[:1000]
        row = LoopContextExpansion(
            expansion_id=opportunity.opportunity_id,
            opportunity_id=opportunity.opportunity_id,
            loop_id=opportunity.loop_id,
            round_id=opportunity.round_id,
            source_context_id=opportunity.source.context_id,
            source_revision_id=opportunity.source.revision_id,
            policy_version=policy_version,
            level=level,
            independence_key=opportunity.independence_key,
            semantic_fingerprint=opportunity.semantic_fingerprint,
            workspace_mode=opportunity.workspace_mode,
            opportunity=opportunity.model_dump(mode="json"),
            safe_summary=summary,
            correlation_id=correlation_id or opportunity.opportunity_id,
            causation_id=causation_id,
        )
        session.add(row)
        await session.flush()
        session.add(
            LoopContextExpansionTransition(
                transition_id=uuid.uuid4().hex,
                expansion_id=row.expansion_id,
                loop_id=row.loop_id,
                round_id=row.round_id,
                revision=1,
                from_state="detected",
                to_state="detected",
                safe_summary=summary,
            )
        )
        await self._event(session, row)
        return row

    async def transition(
        self,
        session: AsyncSession,
        expansion_id: str,
        target: str,
        summary: str,
        *,
        blocker_code: ExpansionBlockerCode | None = None,
        result: dict | None = None,
    ) -> LoopContextExpansion:
        row = await session.get(LoopContextExpansion, expansion_id, with_for_update=True)
        if row is None:
            raise LookupError("Context expansion 不存在")
        if row.state == target:
            return row
        try:
            self._machine.validate(row.state, target)
        except ExpansionTransitionRejected as exc:
            raise ExpansionRepositoryRejected(str(exc)) from exc
        previous = row.state
        row.state = target
        row.revision += 1
        row.safe_summary = summary[:1000]
        row.blocker_code = blocker_code
        if result is not None:
            row.result = {**(row.result or {}), **result}
        if target in self._TERMINAL:
            row.completed_at = datetime.now(UTC)
        session.add(
            LoopContextExpansionTransition(
                transition_id=uuid.uuid4().hex,
                expansion_id=row.expansion_id,
                loop_id=row.loop_id,
                round_id=row.round_id,
                revision=row.revision,
                from_state=previous,
                to_state=target,
                blocker_code=blocker_code,
                safe_summary=row.safe_summary,
                result=row.result,
            )
        )
        await self._event(session, row)
        return row

    async def by_round(self, session: AsyncSession, round_id: str) -> tuple[LoopContextExpansion, ...]:
        return tuple(
            (
                await session.scalars(
                    select(LoopContextExpansion)
                    .where(LoopContextExpansion.round_id == round_id)
                    .order_by(LoopContextExpansion.created_at, LoopContextExpansion.expansion_id)
                )
            ).all()
        )

    async def by_directive(self, session: AsyncSession, directive_id: str) -> LoopContextExpansion | None:
        return await session.scalar(
            select(LoopContextExpansion).where(
                LoopContextExpansion.result["directive_id"].astext == directive_id
            )
        )

    async def active_independence_keys(
        self,
        session: AsyncSession,
        loop_id: str,
        *,
        exclude_round_id: str | None = None,
    ) -> frozenset[str]:
        pending = select(LoopContextExpansion.independence_key).where(
            LoopContextExpansion.loop_id == loop_id,
            LoopContextExpansion.state.not_in(self._TERMINAL),
        )
        active_lane = (
            select(LoopContextExpansion.independence_key)
            .join(
                LoopContextMembership,
                (LoopContextMembership.loop_id == LoopContextExpansion.loop_id)
                & (LoopContextMembership.lane_id == LoopContextExpansion.result["lane_id"].astext),
            )
            .join(CurationLane, CurationLane.lane_id == LoopContextMembership.lane_id)
            .where(
                LoopContextExpansion.loop_id == loop_id,
                LoopContextExpansion.state == "dispatched",
                LoopContextMembership.status == "active",
                CurationLane.lifecycle == "active",
            )
        )
        if exclude_round_id is not None:
            pending = pending.where(LoopContextExpansion.round_id != exclude_round_id)
            active_lane = active_lane.where(LoopContextExpansion.round_id != exclude_round_id)
        pending_values = await session.scalars(pending)
        active_values = await session.scalars(active_lane)
        return frozenset((*pending_values.all(), *active_values.all()))

    async def _event(self, session: AsyncSession, row: LoopContextExpansion) -> None:
        await self._journal.append(
            session,
            row.loop_id,
            CanonicalEventDraft(
                kind=f"context_expansion.{row.state}",
                entity_type="context_expansion",
                entity_id=row.expansion_id,
                entity_revision=row.revision,
                correlation_id=row.correlation_id,
                causation_id=row.causation_id,
                payload={
                    "expansion_id": row.expansion_id,
                    "opportunity_id": row.opportunity_id,
                    "round_id": row.round_id,
                    "source_context_id": row.source_context_id,
                    "source_revision_id": row.source_revision_id,
                    "state": row.state,
                    "level": row.level,
                    "policy_version": row.policy_version,
                    "workspace_mode": row.workspace_mode,
                    "independence_key": row.independence_key,
                    "safe_summary": row.safe_summary,
                    "blocker_code": row.blocker_code,
                    "result": row.result,
                },
                idempotency_key=f"context-expansion:{row.expansion_id}:revision:{row.revision}",
            ),
        )
