r"""本文件对外提供 PortfolioSemanticIndexService 与 PortfolioIndexBuildResult。

输入为冻结 Observation、Revision reader、监督模型 factories 和并发上限；输出为完整 indexes、catalog、stage 与真实用量。
具体工作流为冻结独立局部／整体合同，精确缓存优先，权威追加继承局部 records，再用完整目录及任意冻结原文做整体解释和联合验证。
模型工作不持有事务；短事务解析局部及完整 Index 赢家并校验 catalog。全部阶段共用 Loop 准入／结算，取消和竞争仍计实际成本。
新 Index 包含 interpretation proof；必要综合失败不发布局部-only artifact。显式新授权重试记录版本，冻结认知输入不变。
Revision 原文从版本化 semantic view 读取，控制与 reasoning 不参与 segmentation、fallback 或综合解释。
示例：result = await service.build(observation)；稳定旧段免重抽，新增否定与远端怀疑生成联合诊断，消费者读取一个完整目标。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.context_expansion.artifact_repository import (
    SemanticDerivationArtifactRepository,
)
from backend.app.desktop.agent_loop.context_expansion.contracts import (
    DerivationStageRecord,
)
from backend.app.desktop.agent_loop.context_expansion.mission_sections import (
    FrozenMissionSectionCatalog,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_grounding import (
    SemanticClaimSupportProposal,
    SemanticGroundingValidator,
    SemanticProjectionProposal,
    SupervisedSemanticClaimSupportVerifier,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_index import (
    RevisionSemanticIndex,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import (
    RevisionSemanticIndexer,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
    PortfolioIndexCatalog,
    PortfolioIndexCatalogOverflow,
)
from backend.app.desktop.agent_loop.context_expansion.stage_telemetry import (
    DerivationStageTimer,
)
from backend.app.desktop.agent_loop.expansion_resource_policy import (
    resolve_expansion_resources,
)
from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger
from backend.app.desktop.context_evolution import (
    ContextRevisionIdentityMismatch,
    ContextRevisionReader,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.context_evolution.repository import ContextRevisionNotFound

from .contracts import stable_expansion_hash
from .index_assembly import assemble_revision_index
from .index_budget_repository import IndexBudgetReservationRepository
from .index_inheritance import RevisionIndexInheritancePlanner
from .index_model_budget import (
    BudgetedIndexModel,
    IndexBudgetExceeded,
    IndexModelBudget,
)
from .interpretation_record import (
    RevisionInterpretationProposal,
    RevisionInterpretationRecord,
)
from .projection_record_repository import ProjectionRecordRepository
from .revision_interpretation import (
    INTERPRETATION_PROMPT,
    RevisionInterpretationBuilder,
)
from .segment_projection import (
    SEGMENT_PROMPT,
    SegmentProjectionBuilder,
    SegmentProjectionRecord,
)


@dataclass
class _IndexBuildState:
    attempts: dict = field(default_factory=dict)
    records: dict = field(default_factory=dict)
    telemetry: dict = field(default_factory=dict)
    budget: Any = None
    contract: str = ""
    local_contract: str = ""
    interpretation_contract: str = ""
    model_identities: dict = field(default_factory=dict)
    budget_authorization: dict = field(default_factory=dict)


_BUILD_STATE: ContextVar[_IndexBuildState] = ContextVar("semantic_index_build")


class PortfolioIndexBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    indexes: tuple[RevisionSemanticIndex, ...] = ()
    catalog: PortfolioIndexCatalog | None = None
    blocker_code: str | None = None
    blocker_summary: str | None = None
    stage_record: DerivationStageRecord | None = None


class PortfolioSemanticIndexService:
    VERSION = "portfolio-semantic-index-service-v4"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        *,
        concurrency: int = 4,
        max_attempts: int = 2,
        catalog_max_descriptor_chars: int | None = None,
        semantic_projector_factory: Callable[[], Any] | None = None,
        semantic_claim_verifier_factory: Callable[[], Any] | None = None,
        semantic_interpreter_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._sessions = sessions
        self._revision_repository = ContextRevisionRepository()
        self._reader = ContextRevisionReader(self._revision_repository, checkpointer)
        self._artifacts = SemanticDerivationArtifactRepository()
        self._indexer = RevisionSemanticIndexer()
        self._concurrency = max(1, concurrency)
        self._max_attempts = max(1, max_attempts)
        self._catalog_max_descriptor_chars = catalog_max_descriptor_chars
        self._semantic_projector_factory = semantic_projector_factory
        self._semantic_claim_verifier_factory = semantic_claim_verifier_factory
        self._semantic_interpreter_factory = (
            semantic_interpreter_factory or semantic_projector_factory
        )
        self._records = ProjectionRecordRepository()

    @property
    def _projection_contract(self):
        return _BUILD_STATE.get().contract

    def _prepare_contract(self):
        if not hasattr(self, "_indexer"):
            return
        state = _BUILD_STATE.get()
        for factory in (
            self._semantic_projector_factory,
            self._semantic_claim_verifier_factory,
            self._semantic_interpreter_factory,
        ):
            state.model_identities[factory] = self._model_identity(factory)
        state.local_contract = (
            "loc:"
            + stable_expansion_hash(
                "segment-local-projection-v2",
                SegmentProjectionRecord.SCHEMA_VERSION,
                self._indexer.INDEX_SCHEMA_VERSION,
                self._indexer.segmenter_version,
                SEGMENT_PROMPT,
                SupervisedSemanticClaimSupportVerifier.VERSION,
                SemanticGroundingValidator.VERSION,
                SemanticProjectionProposal.model_json_schema(),
                SemanticClaimSupportProposal.model_json_schema(),
                state.model_identities[self._semantic_projector_factory],
                state.model_identities[self._semantic_claim_verifier_factory],
            )[:60]
        )
        state.interpretation_contract = (
            "syn:"
            + stable_expansion_hash(
                "frozen-evidence-discovery-v1",
                RevisionInterpretationRecord.SCHEMA_VERSION,
                RevisionInterpretationProposal.model_json_schema(),
                INTERPRETATION_PROMPT,
                state.model_identities[self._semantic_interpreter_factory],
                state.model_identities[self._semantic_claim_verifier_factory],
                state.budget.resources.policy.model_dump(mode="json"),
                SemanticGroundingValidator.VERSION,
                SupervisedSemanticClaimSupportVerifier.VERSION,
            )[:60]
        )
        state.contract = (
            "rev:"
            + stable_expansion_hash(
                "revision-projection-contract-v1",
                state.local_contract,
                state.interpretation_contract,
            )[:60]
        )

    @staticmethod
    def _model_identity(factory):
        if factory is None:
            return "no-model"
        model = factory()
        return getattr(
            model,
            "cache_identity",
            f"{type(model).__module__}.{type(model).__qualname__}",
        )

    @property
    def _projection_attempts(self):
        return _BUILD_STATE.get().attempts

    @_projection_attempts.setter
    def _projection_attempts(self, value):
        _BUILD_STATE.set(_IndexBuildState(attempts=value))

    async def build(
        self,
        observation: LoopObservationEnvelope,
        *,
        budget_authority_revision: int | None = None,
    ) -> PortfolioIndexBuildResult:
        self._projection_attempts = {}
        timer = DerivationStageTimer(
            "portfolio_indexing",
            (observation.observed_frontier_hash,),
            self.VERSION,
        )
        frontier = tuple(
            item for item in observation.portfolio_frontier if item.get("revision")
        )
        if not frontier:
            return await self._result(
                observation,
                timer,
                blocker_code="portfolio_index_empty",
                blocker_summary="冻结 Portfolio 没有可建立 semantic index 的 Revision",
            )
        semaphore = asyncio.Semaphore(self._concurrency)
        try:
            _BUILD_STATE.get().budget = await self._model_budget(
                observation, budget_authority_revision
            )
            self._prepare_contract()
            tasks = [
                asyncio.create_task(self._bounded_index(observation, item, semaphore))
                for item in frontier
            ]
            try:
                indexes = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            self._catalog(observation, indexes)
            async with self._sessions.begin() as session:
                indexes = tuple(
                    [
                        await self._publish_one(session, index)
                        for index in sorted(indexes, key=lambda i: i.source.revision_id)
                    ]
                )
                catalog = self._catalog(observation, indexes)
        except asyncio.CancelledError:
            raise
        except PortfolioIndexCatalogOverflow as exc:
            return await self._result(
                observation,
                timer,
                blocker_code="portfolio_catalog_overflow",
                blocker_summary=str(exc)[:1600],
            )
        except (
            ContextRevisionNotFound,
            ContextRevisionIdentityMismatch,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            return await self._result(
                observation,
                timer,
                blocker_code="portfolio_index_budget"
                if isinstance(exc, IndexBudgetExceeded)
                else "portfolio_index_failed",
                blocker_summary=f"完整 Revision semantic index 构建失败: {str(exc)[:1600]}",
            )
        finally:
            if hasattr(self, "_sessions"):
                attempts = tuple(
                    a for rows in self._projection_attempts.values() for a in rows
                )
                await asyncio.shield(
                    self._record_attempt_usage(
                        getattr(observation, "loop_id", ""), attempts
                    )
                )
        return await self._result(
            observation,
            timer,
            indexes=tuple(indexes),
            catalog=catalog,
        )

    async def _model_budget(self, observation, authority_revision=None):
        snapshot = getattr(observation, "budget", {}) or {}
        revision = getattr(observation, "authority_revision", 1)
        if authority_revision is not None:
            revision = authority_revision
            snapshot = await IndexBudgetReservationRepository.authorization_snapshot(
                self._sessions, observation.loop_id, revision
            )
        limits = dict(snapshot.get("limits") or {})
        usage = dict(snapshot.get("usage") or {})
        resources = resolve_expansion_resources(limits, revision, usage)
        _BUILD_STATE.get().budget_authorization = {
            "grant_revision": revision,
            "limits": limits,
            "usage": usage,
        }
        return IndexModelBudget(
            resources,
            reservations=IndexBudgetReservationRepository(
                self._sessions,
                getattr(observation, "loop_id", ""),
                resources.grant_revision,
                limits,
                usage,
            ),
        )

    def _catalog(self, observation, indexes):
        return PortfolioIndexCatalog.create(
            frontier_hash=observation.observed_frontier_hash,
            indexes=tuple(indexes),
            mission_catalog=(
                FrozenMissionSectionCatalog.from_mission(
                    observation.loop_id,
                    EffectiveMissionProjector.from_observation(observation),
                )
                if (getattr(observation, "mission", None) or {}).get(
                    "completion_checks"
                )
                or (getattr(observation, "goal", None) or {}).get("acceptance_criteria")
                else None
            ),
            max_descriptor_chars=self._catalog_capacity(observation),
        )

    async def _publish_one(self, session, index):
        records = _BUILD_STATE.get().records.get(index.source.revision_id)
        if records is not None:
            cached = await self._artifacts.cached_index(
                session,
                revision_id=index.source.revision_id,
                source_content_hash=index.source_content_hash,
                index_schema_version=index.index_schema_version,
                segmenter_version=index.segmenter_version,
                projector_version=index.projector_version,
            )
            if cached is not None:
                return cached
            winners = tuple(
                [
                    await self._records.put(session, index.source.context_id, record)
                    for record in records
                ]
            )
            committed = await self._artifacts.cached_index(
                session,
                revision_id=index.source.revision_id,
                source_content_hash=index.source_content_hash,
                index_schema_version=index.index_schema_version,
                segmenter_version=index.segmenter_version,
                projector_version=index.projector_version,
            )
            if committed is not None:
                return committed
            index = assemble_revision_index(
                self._indexer,
                index,
                winners,
                index.inheritance,
                interpretation=index.interpretation,
                local_contract=_BUILD_STATE.get().local_contract,
            )
        row = await self._artifacts.put_index(
            session,
            index,
            attempt_records=self._projection_attempts.get(index.source.revision_id, ()),
        )
        return (
            RevisionSemanticIndex.model_validate(row.payload)
            if row is not None
            else index
        )

    def _catalog_capacity(self, observation: LoopObservationEnvelope) -> int | None:
        observed_budget = getattr(observation, "budget", {}) or {}
        authority = _BUILD_STATE.get().budget_authorization
        policy = resolve_expansion_resources(
            authority.get("limits", dict(observed_budget.get("limits") or {})),
            authority.get(
                "grant_revision", getattr(observation, "authority_revision", 1)
            ),
            authority.get("usage", dict(observed_budget.get("usage") or {})),
        ).policy
        capacities = [value for value in (
            policy.max_catalog_descriptor_chars, self._catalog_max_descriptor_chars,
        ) if value is not None]
        return min(capacities) if capacities else None

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
            blocker_summary
            or f"已准备 {len(indexes)} 个完整 Revision semantic indexes",
            failure_code=blocker_code,
        )
        scope_fingerprint = stable_expansion_hash(
            "index-stage-scope",
            _BUILD_STATE.get().contract,
            getattr(getattr(self, "_indexer", None), "segmenter_version", ""),
            getattr(observation, "authority_revision", 1),
            getattr(observation, "budget", {}),
            _BUILD_STATE.get().budget_authorization,
            blocker_code or "ready",
        )
        record = DerivationStageRecord.model_validate(
            {
                **record.model_dump(mode="json"),
                "input_identities": (*record.input_identities, scope_fingerprint),
            }
        )
        for revision_id, telemetry in _BUILD_STATE.get().telemetry.items():
            self._append_attempt(
                revision_id, {"role": "semantic_index_build", **telemetry}
            )
        attempts = tuple(
            item
            for revision_attempts in self._projection_attempts.values()
            for item in revision_attempts
        )
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
                published = await self._artifacts.put_stage_artifact(
                    session,
                    loop_id=observation.loop_id,
                    round_id=observation.round_id,
                    stage=record.stage,
                    input_identities=record.input_identities,
                    version=record.version,
                    outcome="blocked" if blocker_code else "ready",
                    payload=record.model_dump(mode="json"),
                    attempt_records=attempts,
                    adopt_winner=True,
                )
                if published is not None:
                    record = DerivationStageRecord.model_validate(published.payload)
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
            revision_id = str(
                (frontier_item.get("revision") or {}).get("revision_id") or "unknown"
            )
            for attempt in range(1, self._max_attempts + 1):
                try:
                    index = await self._index_one(observation, frontier_item)
                    self._append_attempt(
                        revision_id,
                        {
                            "role": "semantic_index_builder",
                            "attempt": attempt,
                            "outcome": "ready",
                        },
                    )
                    return index
                except asyncio.CancelledError:
                    raise
                except (
                    ContextRevisionNotFound,
                    ContextRevisionIdentityMismatch,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as exc:
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

    async def _read_and_plan(self, observation, frontier_item):
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
                projector_version=self._projection_contract,
            )
            if cached is not None:
                if cached.source != source:
                    raise ValueError("semantic index cached source identity mismatch")
                _BUILD_STATE.get().telemetry[source.revision_id] = {
                    "mode": "exact_hit",
                    "reused_segments": len(cached.segments),
                    "recomputed_segments": 0,
                }
                return cached, None, ()
            view = await self._reader.read(session, source, "semantic")
            frozen = self._indexer.index(
                source=source,
                source_content_hash=frozen_hash,
                context_role=str(frontier_item.get("role") or "context"),
                active_objective=str(
                    (observation.mission or observation.goal or {}).get("outcome")
                    or "Current mission"
                ),
                raw_messages=tuple(view.messages),
                projector_version=self._projection_contract,
            )
            planner = RevisionIndexInheritancePlanner(
                self._revision_repository,
                self._artifacts,
                self._records,
                self._projection_contract,
                local_contract=_BUILD_STATE.get().local_contract,
            )
            plan, inherited = await planner.plan(session, revision, frozen)
        return frozen, plan, inherited

    async def _index_one(self, observation, frontier_item) -> RevisionSemanticIndex:
        frozen, plan, inherited = await self._read_and_plan(observation, frontier_item)
        if plan is None:
            return frozen
        source = frozen.source
        records = list(inherited)
        for segment in frozen.segments[len(inherited) :]:
            messages = tuple(
                m for m in frozen.messages if m.message_id in set(segment.message_ids)
            )
            builder = SegmentProjectionBuilder(
                source.context_id,
                _BUILD_STATE.get().local_contract,
                self._budgeted_model(self._semantic_projector_factory),
                self._budgeted_model(self._semantic_claim_verifier_factory),
                lambda attempts: [
                    self._append_attempt(
                        source.revision_id, {**a, "index_phase": "local"}
                    )
                    for a in attempts
                ],
            )
            records.append(await builder.build(segment, messages))
        async with self._sessions.begin() as session:
            records = await self._records.resolve_existing(
                session, source.context_id, records
            )
        state = _BUILD_STATE.get()
        interpreter = RevisionInterpretationBuilder(
            self._budgeted_model(self._semantic_interpreter_factory),
            self._budgeted_model(self._semantic_claim_verifier_factory),
            state.budget.resources,
            state.interpretation_contract,
            state.local_contract,
            lambda attempts: [
                self._append_attempt(source.revision_id, a) for a in attempts
            ],
        )
        interpretation = await interpreter.build(frozen, records, plan)
        _BUILD_STATE.get().records[source.revision_id] = tuple(records)
        _BUILD_STATE.get().telemetry[source.revision_id] = {
            "mode": plan.mode,
            "baseline_index_id": plan.baseline_index_id,
            "full_build_reason": plan.full_build_reason,
            "reused_segments": len(plan.reused_segment_ids),
            "recomputed_segments": len(plan.recomputed_segment_ids),
            "read_message_count": len(frozen.messages),
            "interpretation_original_segments": len(interpretation.provided),
            "interpretation_read_rounds": len(interpretation.read_requests),
        }
        return assemble_revision_index(
            self._indexer,
            frozen,
            tuple(records),
            plan,
            interpretation=interpretation,
            local_contract=state.local_contract,
        )

    def _budgeted_model(self, factory):
        if factory is None:
            return None
        model = factory()
        actual = getattr(
            model,
            "cache_identity",
            f"{type(model).__module__}.{type(model).__qualname__}",
        )
        if actual != _BUILD_STATE.get().model_identities[factory]:
            raise ValueError("semantic index model configuration 已 stale")
        return BudgetedIndexModel(model, _BUILD_STATE.get().budget)

    def _append_attempt(self, revision_id: str, attempt: dict[str, Any]) -> None:
        self._projection_attempts[revision_id] = (
            *self._projection_attempts.get(revision_id, ()),
            attempt,
        )

    @staticmethod
    def _failure_policy(exc: Exception) -> tuple[str, bool]:
        if isinstance(exc, IndexBudgetExceeded):
            return "authorized_budget", False
        if "integrity" in str(exc).casefold():
            return "artifact_integrity", False
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
        delta = LoopUsageDelta(
            model_calls=sum(
                max(0, int(item.get("model_calls") or 0)) for item in attempts
            ),
            input_tokens=sum(
                max(0, int(item.get("input_tokens") or 0)) for item in attempts
            ),
            output_tokens=sum(
                max(0, int(item.get("output_tokens") or 0)) for item in attempts
            ),
            retries=sum(1 for item in attempts if int(item.get("attempt") or 1) > 1),
        )
        budget = _BUILD_STATE.get().budget
        if budget is not None and budget.has_reservations:
            await budget.settle(delta)
        else:
            await LoopUsageLedger(self._sessions).record(loop_id, delta)
