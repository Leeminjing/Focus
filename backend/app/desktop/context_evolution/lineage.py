r"""本文件对外提供 ContextLineageResolver、ContextLineageEdge 与 ContextLineageSource。

对外提供:
    ContextLineageResolver.resolve — 读模型消费的跨 Context 派生边集合
    ContextLineageResolver.external_source_refs — 单条 revision 自身链之外的来源 revision 引用
    ContextLineageEdge — 一条派生边（来源/目标 Context 与其 revision 身份、目标代次）
    ContextLineageSource — 回溯命中的一条跨 Context 来源边（未去重、按命中顺序）

输入为只读 AsyncSession 与各 Context 的当前 revision（读模型），或一条已加载的 revision 合同（展示）。
输出为去重后的跨 Context 派生边，只保留来源仍是本次 Context 集合成员的边；或沿来源链命中顺序排列的来源 revision 引用。

具体工作流为以给定 revision 为种子按 position 顺序逐条读取它的来源边：来源属于同一 Context 时沿该来源继续回溯，
属于其它 Context 时记为派生边并停止深入；resolve 按 (来源 Context, 目标 Context) 去重并保留首次命中，目标 revision
记为承载该来源边的那条 revision。同一 Context 的 revision 链因此永远不会成为拓扑边，也不会出现自环。

示例:
    edges = await ContextLineageResolver().resolve(session, {"c1": "r1"})
    refs = await ContextLineageResolver().external_source_refs(session, revision)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.context_evolution.models import ContextRevision, ContextRevisionSource
from backend.app.desktop.context_evolution.schemas import ContextRevisionContract, ContextRevisionRef


@dataclass(frozen=True, slots=True)
class ContextLineageSource:
    target_context_id: str
    target_revision_id: str
    source_context_id: str
    source_revision_id: str


@dataclass(frozen=True, slots=True)
class ContextLineageEdge:
    source_context_id: str
    source_revision_id: str
    target_context_id: str
    target_revision_id: str
    target_generation: int

    @property
    def entity_id(self) -> str:
        return f"{self.source_context_id}:{self.target_context_id}"

    def payload(self) -> dict[str, Any]:
        return {
            "source_context_id": self.source_context_id,
            "source_revision_id": self.source_revision_id,
            "target_context_id": self.target_context_id,
            "target_revision_id": self.target_revision_id,
        }


class ContextLineageResolver:
    async def resolve(
        self,
        session: AsyncSession,
        current: Mapping[str, str],
    ) -> tuple[ContextLineageEdge, ...]:
        seeds = {context_id: (revision_id,) for context_id, revision_id in current.items() if revision_id}
        origins: dict[str, dict[str, ContextLineageSource]] = {}
        for item in await self.derive(session, seeds):
            if item.source_context_id not in current:
                continue
            origins.setdefault(item.target_context_id, {}).setdefault(item.source_context_id, item)
        if not origins:
            return ()
        generations = await self._generations(
            session,
            {item.target_revision_id for group in origins.values() for item in group.values()},
        )
        return tuple(
            ContextLineageEdge(
                source_context_id=item.source_context_id,
                source_revision_id=item.source_revision_id,
                target_context_id=target_context_id,
                target_revision_id=item.target_revision_id,
                target_generation=generations.get(item.target_revision_id, 1),
            )
            for target_context_id, group in origins.items()
            for item in group.values()
        )

    async def external_source_refs(
        self,
        session: AsyncSession,
        revision: ContextRevisionContract | None,
    ) -> tuple[ContextRevisionRef, ...]:
        if revision is None:
            return ()
        ordered: list[str] = []
        for item in await self.derive(session, {revision.ref.context_id: (revision.ref.revision_id,)}):
            if item.source_revision_id not in ordered:
                ordered.append(item.source_revision_id)
        if not ordered:
            return ()
        rows = list(
            (await session.scalars(select(ContextRevision).where(ContextRevision.revision_id.in_(ordered)))).all()
        )
        by_revision_id = {row.revision_id: row for row in rows}
        return tuple(self._ref(by_revision_id[revision_id]) for revision_id in ordered if revision_id in by_revision_id)

    async def derive(
        self,
        session: AsyncSession,
        seeds: Mapping[str, Iterable[str]],
    ) -> tuple[ContextLineageSource, ...]:
        """按 position 深度优先回溯种子 revision，返回命中的全部跨 Context 来源边（未去重）。"""

        derived: list[ContextLineageSource] = []
        visited: set[str] = set()

        async def visit(context_id: str, revision_id: str) -> None:
            if revision_id in visited:
                return
            visited.add(revision_id)
            rows = list(
                (
                    await session.scalars(
                        select(ContextRevisionSource)
                        .where(ContextRevisionSource.target_revision_id == revision_id)
                        .order_by(ContextRevisionSource.position)
                    )
                ).all()
            )
            for row in rows:
                if row.source_context_id == context_id:
                    await visit(context_id, row.source_revision_id)
                    continue
                derived.append(
                    ContextLineageSource(
                        target_context_id=context_id,
                        target_revision_id=revision_id,
                        source_context_id=row.source_context_id,
                        source_revision_id=row.source_revision_id,
                    )
                )

        for context_id, revision_ids in seeds.items():
            for revision_id in revision_ids:
                await visit(context_id, revision_id)
        return tuple(derived)

    @staticmethod
    async def _generations(session: AsyncSession, revision_ids: set[str]) -> dict[str, int]:
        if not revision_ids:
            return {}
        rows = (
            await session.execute(
                select(ContextRevision.revision_id, ContextRevision.generation).where(
                    ContextRevision.revision_id.in_(revision_ids)
                )
            )
        ).all()
        return {revision_id: int(generation) for revision_id, generation in rows}

    @staticmethod
    def _ref(row: ContextRevision) -> ContextRevisionRef:
        return ContextRevisionRef(
            context_id=row.context_id,
            revision_id=row.revision_id,
            generation=row.generation,
            execution_thread_id=row.execution_thread_id,
            checkpoint_ns=row.checkpoint_ns,
            checkpoint_id=row.checkpoint_id,
            payload_mode=row.payload_mode,
        )
