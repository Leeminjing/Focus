r"""本文件对外提供 PortfolioSemanticIndexService 与 PortfolioIndexBuildResult。

输入为冻结 LoopObservationEnvelope、Context Revision repository/checkpointer、受监督 projector factory 和并发上限；输出为全部授权
Revision 的 ready semantic indexes、PortfolioIndexCatalog 与真实 stage/attempt telemetry，或显式 blocker。具体工作流为并发读取精确
Revision，命中版本化缓存或覆盖全部消息，依次调用无权 projector 和独立 claim verifier，隔离可选 unit 故障并累计真实模型用量，
只有权威来源均成功后才原子发布；取消、重试耗尽和 stale source 不发布部分结果。示例：`result = await service.build(observation)`。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.context_expansion.artifact_repository import (
    SemanticDerivationArtifactRepository,
)
from backend.app.desktop.agent_loop.context_expansion.contracts import (
    DerivationStageRecord,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_index import (
    RevisionSemanticIndex,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
    RevisionSemanticIndexer,
    SupervisedSegmentSemanticProjector,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_grounding import (
    SemanticGroundingValidator,
    SupervisedSemanticClaimSupportVerifier,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
    PortfolioIndexCatalog,
    PortfolioIndexCatalogOverflow,
)
from backend.app.desktop.agent_loop.context_expansion.stage_telemetry import (
    DerivationStageTimer,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger
from backend.app.desktop.context_evolution import (
    ContextRevisionIdentityMismatch,
    ContextRevisionReader,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.context_evolution.repository import ContextRevisionNotFound


class PortfolioIndexBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    indexes: tuple[RevisionSemanticIndex, ...] = ()
    catalog: PortfolioIndexCatalog | None = None
    blocker_code: str | None = None
    blocker_summary: str | None = None
    stage_record: DerivationStageRecord | None = None


class PortfolioSemanticIndexService:
    VERSION = "portfolio-semantic-index-service-v2"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        *,
        concurrency: int = 4,
        max_attempts: int = 2,
        catalog_max_descriptor_chars: int = 64000,
        semantic_projector_factory: Callable[[], Any] | None = None,
        semantic_claim_verifier_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._sessions = sessions
        self._revision_repository = ContextRevisionRepository()
        self._reader = ContextRevisionReader(self._revision_repository, checkpointer)
        self._artifacts = SemanticDerivationArtifactRepository()
        self._indexer = RevisionSemanticIndexer()
        self._concurrency = max(1, concurrency)
        self._max_attempts = max(1, max_attempts)
        self._catalog_max_descriptor_chars = max(1, catalog_max_descriptor_chars)
        self._semantic_projector_factory = semantic_projector_factory
        self._semantic_claim_verifier_factory = semantic_claim_verifier_factory
        self._projection_attempts: dict[str, tuple[dict[str, Any], ...]] = {}

    async def build(self, observation: LoopObservationEnvelope) -> PortfolioIndexBuildResult:
        self._projection_attempts = {}
        timer = DerivationStageTimer(
            "portfolio_indexing",
            (observation.observed_frontier_hash,),
            "portfolio-semantic-index-service-v1",
        )
        frontier = tuple(item for item in observation.portfolio_frontier if item.get("revision"))
        if not frontier:
            return await self._result(
                observation,
                timer,
                blocker_code="portfolio_index_empty",
                blocker_summary="冻结 Portfolio 没有可建立 semantic index 的 Revision",
            )
        semaphore = asyncio.Semaphore(self._concurrency)
        try:
            indexes = await asyncio.gather(
                *(self._bounded_index(observation, item, semaphore) for item in frontier)
            )
            catalog = PortfolioIndexCatalog.create(
                frontier_hash=observation.observed_frontier_hash,
                indexes=tuple(indexes),
                max_descriptor_chars=self._catalog_max_descriptor_chars,
            )
            async with self._sessions.begin() as session:
                for index in indexes:
                    await self._artifacts.put_index(
                        session,
                        index,
                        attempt_records=self._projection_attempts.get(index.source.revision_id, ()),
                    )
        except asyncio.CancelledError:
            raise
        except PortfolioIndexCatalogOverflow as exc:
            return await self._result(
                observation,
                timer,
                blocker_code="portfolio_catalog_overflow",
                blocker_summary=str(exc)[:1600],
            )
        except (ContextRevisionNotFound, ContextRevisionIdentityMismatch, KeyError, TypeError, ValueError) as exc:
            return await self._result(
                observation,
                timer,
                blocker_code="portfolio_index_failed",
                blocker_summary=f"完整 Revision semantic index 构建失败: {str(exc)[:1600]}",
            )
        return await self._result(
            observation,
            timer,
            indexes=tuple(indexes),
            catalog=catalog,
        )

    async def _result(
        self,
        observation: LoopObservationEnvelope,
        timer: DerivationStageTimer,
        *,
        indexes: tuple[RevisionSemanticIndex, ...] = (),
        catalog: PortfolioIndexCatalog | None = None,
        blocker_code: str | None = None,
        blocker_summary: str | None = None,
    ) -> PortfolioIndexBuildResult:
        record = timer.finish(
            tuple(item.index_id for item in indexes),
            blocker_summary or f"已准备 {len(indexes)} 个完整 Revision semantic indexes",
            failure_code=blocker_code,
        )
        attempts = tuple(
            item
            for revision_attempts in self._projection_attempts.values()
            for item in revision_attempts
        )
        await self._record_attempt_usage(observation.loop_id, attempts)
        async with self._sessions.begin() as session:
            existing = await self._artifacts.stage_artifact(
                session,
                loop_id=observation.loop_id,
                round_id=observation.round_id,
                stage=record.stage,
                input_identities=record.input_identities,
                version=record.version,
            )
            if existing is None:
                await self._artifacts.put_stage_artifact(
                    session,
                    loop_id=observation.loop_id,
                    round_id=observation.round_id,
                    stage=record.stage,
                    input_identities=record.input_identities,
                    version=record.version,
                    outcome="blocked" if blocker_code else "ready",
                    payload=record.model_dump(mode="json"),
                    attempt_records=attempts,
                )
            else:
                record = DerivationStageRecord.model_validate(existing.payload)
        return PortfolioIndexBuildResult(
            indexes=indexes,
            catalog=catalog,
            blocker_code=blocker_code,
            blocker_summary=blocker_summary,
            stage_record=record,
        )

    async def _bounded_index(
        self,
        observation: LoopObservationEnvelope,
        frontier_item: dict[str, Any],
        semaphore: asyncio.Semaphore,
    ) -> RevisionSemanticIndex:
        async with semaphore:
            last_error: Exception | None = None
            revision_id = str((frontier_item.get("revision") or {}).get("revision_id") or "unknown")
            for attempt in range(1, self._max_attempts + 1):
                try:
                    index = await self._index_one(observation, frontier_item)
                    self._append_attempt(
                        revision_id,
                        {"role": "semantic_index_builder", "attempt": attempt, "outcome": "ready"},
                    )
                    return index
                except asyncio.CancelledError:
                    raise
                except (ContextRevisionNotFound, ContextRevisionIdentityMismatch, KeyError, TypeError, ValueError) as exc:
                    last_error = exc
                    category, retryable = self._failure_policy(exc)
                    self._append_attempt(
                        revision_id,
                        {
                            "role": "semantic_index_builder",
                            "attempt": attempt,
                            "outcome": "error",
                            "error_type": type(exc).__name__,
                            "failure_category": category,
                            "retryable": retryable,
                        },
                    )
                    if not retryable:
                        raise
            if last_error is None:
                raise RuntimeError("semantic index attempt 未执行")
            raise last_error

    async def _index_one(
        self,
        observation: LoopObservationEnvelope,
        frontier_item: dict[str, Any],
    ) -> RevisionSemanticIndex:
        source = ContextRevisionRef.model_validate(frontier_item["revision"])
        frozen_hash = str(frontier_item.get("content_hash") or "")
        scope = set((observation.grant or {}).get("context_scope") or ())
        if scope and source.context_id not in scope:
            raise ValueError("semantic index source 超出 delegation context scope")
        async with self._sessions.begin() as session:
            revision = await self._revision_repository.get(session, source)
            if revision.content_hash != frozen_hash:
                raise ValueError("semantic index source content hash 已 stale")
            cached = await self._artifacts.cached_index(
                session,
                revision_id=source.revision_id,
                source_content_hash=frozen_hash,
                index_schema_version=self._indexer.INDEX_SCHEMA_VERSION,
                segmenter_version=self._indexer.segmenter_version,
                projector_version=self._indexer.PROJECTOR_VERSION,
            )
            if cached is not None:
                return cached
            view = await self._reader.read(session, source, "execution")
            objective = str((observation.mission or observation.goal or {}).get("outcome") or "Current mission")
            index = self._indexer.index(
                source=source,
                source_content_hash=frozen_hash,
                context_role=str(frontier_item.get("role") or "context"),
                active_objective=objective,
                raw_messages=tuple(view.messages),
            )
            if self._semantic_projector_factory is not None:
                projector = SupervisedSegmentSemanticProjector(
                    self._semantic_projector_factory()
                )
                try:
                    drafts = await projector.project(index)
                finally:
                    for attempt in projector.attempt_records:
                        self._append_attempt(source.revision_id, attempt)
                assessments = ()
                confirmed = tuple(
                    draft
                    for draft in drafts
                    if draft.authority == "confirmed"
                    and SemanticGroundingValidator.structurally_valid(
                        self._indexer.message_contents(index.messages),
                        draft,
                    )
                )
                if confirmed and self._semantic_claim_verifier_factory is not None:
                    verifier = SupervisedSemanticClaimSupportVerifier(
                        self._semantic_claim_verifier_factory()
                    )
                    try:
                        assessments = await verifier.verify(confirmed)
                    finally:
                        for attempt in verifier.attempt_records:
                            self._append_attempt(source.revision_id, attempt)
                index = self._indexer.index(
                    source=source,
                    source_content_hash=frozen_hash,
                    context_role=str(frontier_item.get("role") or "context"),
                    active_objective=objective,
                    raw_messages=tuple(view.messages),
                    unit_drafts=drafts,
                    claim_support_assessments=assessments,
                )
            return index

    def _append_attempt(self, revision_id: str, attempt: dict[str, Any]) -> None:
        self._projection_attempts[revision_id] = (
            *self._projection_attempts.get(revision_id, ()),
            attempt,
        )

    @staticmethod
    def _failure_policy(exc: Exception) -> tuple[str, bool]:
        if isinstance(exc, ContextRevisionNotFound):
            return "source_not_found", False
        if isinstance(exc, ContextRevisionIdentityMismatch):
            return "stale_source", False
        message = str(exc).casefold()
        if "stale" in message:
            return "stale_source", False
        if "scope" in message or "越出" in message or "超出" in message:
            return "source_out_of_scope", False
        if isinstance(exc, (KeyError, TypeError)):
            return "model_schema_error", True
        return "projection_error", True

    async def _record_attempt_usage(
        self,
        loop_id: str,
        attempts: tuple[dict[str, Any], ...],
    ) -> None:
        if not attempts:
            return
        await LoopUsageLedger(self._sessions).record(
            loop_id,
            LoopUsageDelta(
                model_calls=sum(max(0, int(item.get("model_calls") or 0)) for item in attempts),
                input_tokens=sum(max(0, int(item.get("input_tokens") or 0)) for item in attempts),
                output_tokens=sum(max(0, int(item.get("output_tokens") or 0)) for item in attempts),
                retries=sum(1 for item in attempts if int(item.get("attempt") or 1) > 1),
            ),
        )
