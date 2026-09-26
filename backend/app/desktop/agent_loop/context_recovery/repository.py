r"""本文件对外提供 ContextRecoveryOpportunityRepository 的持久化、查询、失效与原子消费操作。

输入为严格恢复合同、Loop/round/opportunity identity 和提交结果；输出为幂等 ORM 当前行或显式状态转换。具体工作流为按稳定
opportunity identity 插入或重放，重复发现保留首次到期时间并拒绝来源 authority 改写，发布事务内以行锁校验 pending 后一次性写入 decision/lane/context 结果。
示例：`row = await repository.put(session, opportunity, round_id)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.context_recovery.contracts import (
    CONTEXT_RECOVERY_COMPILER_VERSION,
    ContextRecoveryOpportunityContract,
)
from backend.app.desktop.agent_loop.models import LoopContextRecoveryOpportunity


class ContextRecoveryOpportunityRepository:
    async def put(
        self,
        session: AsyncSession,
        opportunity: ContextRecoveryOpportunityContract,
        round_id: str,
    ) -> LoopContextRecoveryOpportunity:
        row = await session.get(
            LoopContextRecoveryOpportunity,
            opportunity.opportunity_id,
            with_for_update=True,
            populate_existing=True,
        )
        if row is not None:
            persisted = ContextRecoveryOpportunityContract.model_validate(row.payload)
            if self._immutable_payload(persisted) != self._immutable_payload(opportunity):
                raise ValueError("同一 Context recovery identity 不得改写冻结 payload")
            return row
        conflicting = await session.scalar(
            select(LoopContextRecoveryOpportunity)
            .where(
                LoopContextRecoveryOpportunity.loop_id == opportunity.loop_id,
                LoopContextRecoveryOpportunity.source_revision_id == opportunity.source.revision_id,
                LoopContextRecoveryOpportunity.source_run_id == opportunity.source_run_id,
                LoopContextRecoveryOpportunity.compiler_version == opportunity.compiler_version,
            )
            .with_for_update()
        )
        if conflicting is not None:
            raise ValueError("同一来源 Run 的 Context recovery authority 已变化")
        row = LoopContextRecoveryOpportunity(
            opportunity_id=opportunity.opportunity_id,
            loop_id=opportunity.loop_id,
            round_id=round_id,
            source_context_id=opportunity.source.context_id,
            source_revision_id=opportunity.source.revision_id,
            source_frontier_hash=opportunity.source_frontier_hash,
            goal_revision=opportunity.goal_revision,
            workspace_revision=opportunity.workspace_revision,
            authority_revision=opportunity.authority_revision,
            grant_id=opportunity.grant_id,
            grant_revision=opportunity.grant_revision,
            source_run_id=opportunity.source_run_id,
            compiler_version=opportunity.compiler_version,
            expires_at=opportunity.expires_at,
            status=opportunity.status,
            safe_summary=opportunity.safe_summary,
            payload=opportunity.model_dump(mode="json"),
        )
        session.add(row)
        await session.flush()
        return row

    async def get_contract(
        self,
        session: AsyncSession,
        opportunity_id: str,
        *,
        for_update: bool = False,
    ) -> ContextRecoveryOpportunityContract | None:
        statement = select(LoopContextRecoveryOpportunity).where(
            LoopContextRecoveryOpportunity.opportunity_id == opportunity_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = await session.scalar(statement)
        return ContextRecoveryOpportunityContract.model_validate(row.payload) if row is not None else None

    async def pending_for_round(self, session: AsyncSession, round_id: str) -> tuple[dict[str, Any], ...]:
        rows = tuple(
            (
                await session.scalars(
                    select(LoopContextRecoveryOpportunity)
                    .where(
                        LoopContextRecoveryOpportunity.round_id == round_id,
                        LoopContextRecoveryOpportunity.status == "pending",
                        LoopContextRecoveryOpportunity.compiler_version == CONTEXT_RECOVERY_COMPILER_VERSION,
                    )
                    .order_by(LoopContextRecoveryOpportunity.created_at, LoopContextRecoveryOpportunity.opportunity_id)
                )
            ).all()
        )
        now = datetime.now(UTC)
        return tuple(
            ContextRecoveryOpportunityContract.model_validate(row.payload).public_payload()
            for row in rows
            if self._aware(row.expires_at) > now
        )

    async def consume(
        self,
        session: AsyncSession,
        opportunity_id: str,
        *,
        decision_id: str,
        lane_id: str,
        context_id: str,
    ) -> None:
        row = await session.scalar(
            select(LoopContextRecoveryOpportunity)
            .where(LoopContextRecoveryOpportunity.opportunity_id == opportunity_id)
            .with_for_update()
        )
        if row is None or row.status != "pending":
            raise ValueError("Context recovery opportunity 不存在或已消费")
        if self._aware(row.expires_at) <= datetime.now(UTC):
            row.status = "stale"
            raise ValueError("Context recovery opportunity 已过期")
        row.status = "consumed"
        row.consumed_by_decision_id = decision_id
        row.result = {"lane_id": lane_id, "context_id": context_id}
        row.consumed_at = datetime.now(UTC)
        row.payload = {**row.payload, "status": "consumed"}

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _immutable_payload(opportunity: ContextRecoveryOpportunityContract) -> dict[str, Any]:
        return opportunity.model_dump(
            mode="json",
            exclude={"expires_at", "status", "safe_summary"},
        )
