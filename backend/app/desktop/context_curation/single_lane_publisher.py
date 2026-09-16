r"""本文件对外提供 SingleLanePortfolioPublisher，把一对一 Curator 候选发布到通用 Portfolio。

输入为 binding identity、版本化来源、合法 Context projection、运行后缀和 replace/no-change 决策；
输出为原子发布的 Portfolio、Lane 与 Context revision 身份。具体工作流为冻结单 Lane frontier，使用
通用 candidate preparer 写 shadow checkpoint，再由 AtomicPortfolioPublisher 一次切换 Context 与
Program current pointer；本模块不读写旧 revision/attempt 状态。示例：`result = await port.publish(...)`。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.context_curation.compiler import CompiledLaneCandidate
from backend.app.desktop.context_curation.portfolio_publisher import (
    AtomicPortfolioPublisher,
    PortfolioCandidatePreparer,
    PortfolioFreezeRequest,
    PortfolioFreezer,
    PortfolioLaneIntent,
)
from backend.app.desktop.context_curation.models import CurationLane, PortfolioLaneAction
from backend.app.desktop.context_curation.service import (
    SingleLaneCurationProgramService,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionPublisher,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.context_evolution.checkpoint_writer import (
    LangGraphContextCheckpointWriter,
)
from backend.app.desktop.context_projection import ContextProjection


class SingleLanePublicationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    portfolio_revision_id: str
    lane_id: str
    context_revision: ContextRevisionRef


class SingleLanePortfolioPublisher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        graph_factory: Any,
    ) -> None:
        revisions = ContextRevisionRepository()
        context_publisher = ContextRevisionPublisher(
            revisions,
            LangGraphContextCheckpointWriter(graph_factory, checkpointer),
        )
        self._sessions = session_factory
        self._revisions = revisions
        self._freezer = PortfolioFreezer(session_factory, revisions)
        self._preparer = PortfolioCandidatePreparer(
            session_factory,
            context_publisher,
            revisions,
        )
        self._publisher = AtomicPortfolioPublisher(session_factory, revisions)

    async def publish(
        self,
        *,
        binding_id: str,
        source_frontier: tuple[ContextRevisionRef, ...],
        projection: ContextProjection,
        suffix_messages: tuple[dict[str, Any], ...],
        outcome: Literal["replace", "no_change"],
    ) -> SingleLanePublicationResult:
        ids = SingleLaneCurationProgramService.identities(binding_id)
        context_id, current = await self._lane_state(ids.lane_id)
        if outcome == "no_change" and current is None:
            raise ValueError("首个策展 revision 不能声明 no_change")
        action = (
            PortfolioLaneAction.KEEP
            if outcome == "no_change"
            else PortfolioLaneAction.UPDATE
        )
        fingerprint = (
            current.content_hash if current is not None and outcome == "no_change"
            else projection.projection_hash
        )
        frozen = await self._freezer.freeze(
            PortfolioFreezeRequest(
                program_id=ids.program_id,
                source_frontier=source_frontier,
                lane_intents=(
                    PortfolioLaneIntent(
                        lane_id=ids.lane_id,
                        action=action,
                        purpose="Continuously curated context",
                        source_allocation=source_frontier,
                        semantic_fingerprint=fingerprint,
                    ),
                ),
                workspace_revision=self._workspace_revision(source_frontier),
            )
        )
        compiled = {}
        suffixes = {}
        if outcome == "replace":
            candidate_id = frozen.candidate_ids[0]
            compiled[candidate_id] = self._compiled(
                ids.lane_id,
                source_frontier,
                projection,
            )
            suffixes[candidate_id] = suffix_messages
        await self._preparer.prepare(
            frozen.portfolio_revision_id,
            compiled,
            suffix_messages=suffixes,
        )
        published = await self._publisher.publish(
            frozen.portfolio_revision_id,
            frozen.controls,
        )
        revision = next(
            item
            for item in published.context_revisions
            if item.context_id == context_id
        )
        return SingleLanePublicationResult(
            portfolio_revision_id=published.portfolio_revision_id,
            lane_id=ids.lane_id,
            context_revision=revision,
        )

    async def _lane_state(
        self,
        lane_id: str,
    ) -> tuple[str, ContextRevisionContract | None]:
        async with self._sessions() as session:
            lane = await session.get(CurationLane, lane_id)
            if lane is None or lane.managed_context_id is None:
                raise ValueError("单 Lane Curation Program 尚无 managed Context")
            current = await self._revisions.current(session, lane.managed_context_id)
            if current is not None and not current.ref.is_runnable:
                raise ValueError("受管 Context 尚无可运行 current revision")
            return lane.managed_context_id, current

    @staticmethod
    def _compiled(
        lane_id: str,
        frontier: tuple[ContextRevisionRef, ...],
        projection: ContextProjection,
    ) -> CompiledLaneCandidate:
        return CompiledLaneCandidate(
            action="update",
            lane_id=lane_id,
            purpose="Continuously curated context",
            source_frontier=frontier,
            authored_messages=tuple(projection.authored_messages),
            execution_messages=tuple(projection.execution_messages),
            message_lineage=(),
            source_dispositions=(),
            definition_hash=projection.definition_hash,
            projection_hash=projection.projection_hash,
            content_hash=projection.projection_hash,
            semantic_fingerprint=projection.projection_hash,
            projection_status="valid",
        )

    @staticmethod
    def _workspace_revision(frontier: tuple[ContextRevisionRef, ...]) -> str:
        return ":".join(item.revision_id for item in frontier)
