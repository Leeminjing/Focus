r"""本文件对外提供 ContextCleanupPlan 与 ContextRevisionRetentionPlanner。

输入为准备物理删除的 Context identity 集合和现有版本化来源边；输出为可硬删除集合与必须保留
的最小墓碑集合。具体工作流为把候选集合外的存活 revision 视为根，从其来源反向传播保护，直至
得到引用闭包；归档和 Portfolio membership 淘汰不进入清理计划，因而不会删除 revision。
示例：`plan = await planner.plan(session, [context_id])`。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.context_evolution.models import (
    ContextRevision,
    ContextRevisionSource,
)


@dataclass(frozen=True)
class ContextCleanupPlan:
    hard_delete: tuple[str, ...]
    tombstones: tuple[str, ...]


class ContextRevisionRetentionPlanner:
    async def plan(
        self,
        session: AsyncSession,
        context_ids: list[str],
    ) -> ContextCleanupPlan:
        ordered = tuple(dict.fromkeys(context_ids))
        candidates = set(ordered)
        if not candidates:
            return ContextCleanupPlan(hard_delete=(), tombstones=())
        edges = await self._candidate_source_edges(session, candidates)
        protected = self._referenced_closure(edges, candidates)
        return ContextCleanupPlan(
            hard_delete=tuple(context_id for context_id in ordered if context_id not in protected),
            tombstones=tuple(context_id for context_id in ordered if context_id in protected),
        )

    async def _candidate_source_edges(
        self,
        session: AsyncSession,
        candidates: set[str],
    ) -> list[tuple[str, str]]:
        return list(
            (
                await session.execute(
                    select(
                        ContextRevisionSource.source_context_id,
                        ContextRevision.context_id,
                    )
                    .join(
                        ContextRevision,
                        ContextRevision.revision_id
                        == ContextRevisionSource.target_revision_id,
                    )
                    .where(ContextRevisionSource.source_context_id.in_(candidates))
                )
            ).all()
        )

    @staticmethod
    def _referenced_closure(
        edges: list[tuple[str, str]],
        candidates: set[str],
    ) -> set[str]:
        protected = {
            source_context_id
            for source_context_id, target_context_id in edges
            if target_context_id not in candidates
        }
        while True:
            expanded = protected | {
                source_context_id
                for source_context_id, target_context_id in edges
                if target_context_id in protected
            }
            if expanded == protected:
                return protected
            protected = expanded
