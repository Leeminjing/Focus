"""本文件对外提供 ContextLineageResolver 与当前跨 Context 来源关系合同。

输入为当前或任务固定的 Context 引用集合；输出为直接来源引用与可显示的 Context 关系边。
具体工作流为从精确合同读取已扁平化的跨 Context 来源，仅消费来源身份，不回溯或保留历史版本正文。
示例：await ContextLineageResolver().resolve(session, current_context_tokens)。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .repository import ContextRevisionRepository
from .schemas import ContextRevisionContract, ContextRevisionRef


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
        self, session: AsyncSession, current: Mapping[str, str]
    ) -> tuple[ContextLineageEdge, ...]:
        repository = ContextRevisionRepository()
        edges = []
        for context_id, token in sorted(current.items()):
            if not token:
                continue
            value = await repository.get_by_id(session, token)
            if value.ref.context_id != context_id:
                raise ValueError("current provenance Context identity mismatch")
            seen = set()
            for source in await self.external_source_refs(session, value):
                if source.context_id in current and source.context_id not in seen:
                    seen.add(source.context_id)
                    edges.append(
                        ContextLineageEdge(
                            source.context_id,
                            source.revision_id,
                            context_id,
                            token,
                            value.ref.generation,
                        )
                    )
        return tuple(edges)

    async def external_source_refs(
        self, session: AsyncSession, revision: ContextRevisionContract | None
    ) -> tuple[ContextRevisionRef, ...]:
        if revision is None:
            return ()
        return tuple(
            edge.source
            for edge in revision.sources
            if edge.source.context_id != revision.ref.context_id
        )

    async def derive(
        self, session: AsyncSession, seeds: Mapping[str, Iterable[str]]
    ) -> tuple[ContextLineageSource, ...]:
        repository = ContextRevisionRepository()
        edges = []
        for context_id, tokens in sorted(seeds.items()):
            for token in tokens:
                value = await repository.get_by_id(session, token)
                if value.ref.context_id != context_id:
                    raise ValueError("provenance Context identity mismatch")
                edges.extend(
                    ContextLineageSource(
                        context_id,
                        token,
                        edge.source.context_id,
                        edge.source.revision_id,
                    )
                    for edge in value.sources
                    if edge.source.context_id != context_id
                )
        return tuple(edges)
