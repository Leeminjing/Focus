r"""本文件对外提供 PortfolioIndexCatalog、SemanticRetrievalQuery、RetrievalCandidate、RetrievalQueryHit、CandidateDescriptorPage、
ExactEvidenceRead、PlanningRetrievalSession、AuthorizedSemanticRetriever 与 PlanningRetrievalSessionController。

输入为冻结 RevisionSemanticIndex 与 Mission section catalog、授权 index identities、结构化查询与硬预算；输出为有理由的
有界 Context/Mission 候选、来源类型化精确 evidence reads 及可持久化 planning session。具体工作流为构建冻结目录，
在授权 scope 内稳定排序 Context segment/unit 与 Mission section，将唯一 entry 与 query hit 分离、冻结候选页面，
按来源身份精读并以操作账本核算 query/read/model/token 消耗；越权、stale、超预算或非法状态均显式失败。
示例：`candidates = retriever.retrieve(session, query, indexes)`。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.desktop.agent_loop.resource_limits import exceeds_limit, remaining_capacity
from backend.app.desktop.agent_loop.context_expansion.contracts import (
    CandidateEvidenceIdentity,
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.expansion_resource_policy import ExpansionResourcePolicy, FrozenExpansionResources
from backend.app.desktop.agent_loop.expansion_usage import ExpansionUsageCharge, ExpansionUsageLedger
from backend.app.desktop.agent_loop.context_expansion.mission_sections import FrozenMissionSectionCatalog
from backend.app.desktop.agent_loop.context_expansion.semantic_index import (
    RevisionIndexDescriptor,
    RevisionSemanticIndex,
)
from backend.app.desktop.context_curation import EvidenceRef, MissionEvidenceRef, NamespacedMessageRef
from backend.app.desktop.context_evolution import ContextRevisionRef

PlanningSessionState = Literal["created", "queried", "evidence_read", "planned", "blocked", "stale"]
RetrievalEntryKind = Literal["segment", "semantic_unit", "mission_section"]


class _RetrievalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PortfolioIndexCatalogOverflow(ValueError):
    pass


class RetrievalBudget(_RetrievalModel):
    max_queries: int | None = Field(ge=0)
    max_candidates: int | None = Field(ge=0)
    max_exact_reads: int | None = Field(ge=0)
    max_model_calls: int | None = Field(ge=0)
    max_tokens: int | None = Field(ge=0)


class RetrievalUsage(_RetrievalModel):
    queries: int = Field(default=0, ge=0)
    candidates: int = Field(default=0, ge=0)
    exact_reads: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    unreported_model_calls: int = Field(default=0, ge=0)
    compiled_evidence_items: int = Field(default=0, ge=0)


class SemanticRetrievalQuery(_RetrievalModel):
    query_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=4000)
    index_ids: tuple[str, ...] = ()
    kinds: tuple[RetrievalEntryKind, ...] = ("semantic_unit", "segment")
    limit: int = Field(default=16, ge=1, le=128)

    @field_validator("index_ids", "kinds", mode="before")
    @classmethod
    def normalize_values(cls, values: Any) -> tuple[Any, ...]:
        return tuple(sorted(set(values or ())))

    @model_validator(mode="after")
    def require_stable_identity(self) -> Self:
        expected = stable_expansion_hash("semantic-retrieval-query", self.text, self.index_ids, self.kinds, self.limit)
        if self.query_id != expected:
            raise ValueError("retrieval query identity 与内容不一致")
        return self

    @classmethod
    def create(
        cls,
        *,
        text: str,
        index_ids: tuple[str, ...] = (),
        kinds: tuple[RetrievalEntryKind, ...] = ("semantic_unit", "segment"),
        limit: int = 16,
    ) -> Self:
        normalized_indexes = tuple(sorted(set(index_ids)))
        normalized_kinds = tuple(sorted(set(kinds)))
        return cls(
            query_id=stable_expansion_hash(
                "semantic-retrieval-query",
                text,
                normalized_indexes,
                normalized_kinds,
                limit,
            ),
            text=text,
            index_ids=normalized_indexes,
            kinds=normalized_kinds,
            limit=limit,
        )


class RetrievalCandidate(_RetrievalModel):
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_version: Literal["query-scoped-v1", "entry-scoped-v2"] = "query-scoped-v1"
    session_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    query_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_kind: RetrievalEntryKind
    source_type: Literal["context", "mission"] = "context"
    source: ContextRevisionRef | None = None
    mission_ref: MissionEvidenceRef | None = None
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_schema_version: str = Field(min_length=1, max_length=64)
    segmenter_version: str = Field(min_length=1, max_length=64)
    projector_version: str = Field(min_length=1, max_length=64)
    descriptor: str = Field(min_length=1, max_length=4000)
    rank: int = Field(ge=1)
    score: float
    reasons: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_stable_identity(self) -> Self:
        if self.source_type == "mission":
            if self.entry_kind != "mission_section" or self.source is not None or self.mission_ref is None or self.mission_ref.identity_version != "mission-section-v2":
                raise ValueError("Mission retrieval candidate 缺少精确 section 来源")
            if self.source_content_hash != self.mission_ref.content_hash:
                raise ValueError("Mission retrieval candidate 来源 hash 不一致")
        elif self.entry_kind == "mission_section" or self.source is None or self.mission_ref is not None:
            raise ValueError("Context retrieval candidate 缺少精确 Revision 来源")
        if self.identity_version == "entry-scoped-v2" and self.session_id is None:
            raise ValueError("新版 retrieval candidate 缺少冻结 session provenance")
        expected = (
            stable_expansion_hash(
                "retrieval-candidate-v2",
                self.index_id,
                self.entry_kind,
                self.entry_id,
                self.source_content_hash,
            )
            if self.identity_version == "entry-scoped-v2"
            else stable_expansion_hash(
                "retrieval-candidate",
                self.query_id,
                self.index_id,
                self.entry_kind,
                self.entry_id,
            )
        )
        if self.candidate_id != expected:
            raise ValueError("retrieval candidate identity 与来源不一致")
        return self

    def evidence_identity(self) -> CandidateEvidenceIdentity:
        if self.identity_version != "entry-scoped-v2" or self.entry_kind not in {"semantic_unit", "mission_section"}:
            raise ValueError("只有新版可精读 candidate 能进入 WorkSpec")
        return CandidateEvidenceIdentity(
            candidate_id=self.candidate_id,
            index_id=self.index_id,
            entry_id=self.entry_id,
            entry_kind=self.entry_kind,
            source_type=self.source_type,
            source=self.source,
            mission_ref=self.mission_ref,
            source_content_hash=self.source_content_hash,
        )


class RetrievalQueryHit(_RetrievalModel):
    hit_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    rank: int = Field(ge=1)
    score: float
    reasons: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_stable_identity(self) -> Self:
        if self.hit_id != stable_expansion_hash(
            "retrieval-query-hit", self.query_id, self.candidate_id, self.rank, self.score, self.reasons
        ):
            raise ValueError("retrieval query hit identity 与内容不一致")
        return self

    @classmethod
    def from_candidate(cls, candidate: RetrievalCandidate) -> Self:
        payload = (candidate.query_id, candidate.candidate_id, candidate.rank, candidate.score, candidate.reasons)
        return cls(
            hit_id=stable_expansion_hash("retrieval-query-hit", *payload),
            query_id=candidate.query_id,
            candidate_id=candidate.candidate_id,
            rank=candidate.rank,
            score=candidate.score,
            reasons=candidate.reasons,
        )


class RetrievalQueryCoverage(_RetrievalModel):
    query_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested: int = Field(ge=1)
    allocated: int = Field(ge=0)
    returned: int = Field(ge=0)
    deduplicated: int = Field(ge=0)
    unvisited: int = Field(ge=0)


class CandidateDescriptorPage(_RetrievalModel):
    page_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordinal: int = Field(ge=0)
    candidate_ids: tuple[str, ...] = Field(min_length=1)
    estimated_input_tokens: int = Field(ge=0)


class ExactEvidenceRead(_RetrievalModel):
    read_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_type: Literal["context", "mission"] = "context"
    source: ContextRevisionRef | None = None
    mission_ref: MissionEvidenceRef | None = None
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_schema_version: str = Field(min_length=1, max_length=64)
    segmenter_version: str = Field(min_length=1, max_length=64)
    projector_version: str = Field(min_length=1, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...]
    content: Any

    @model_validator(mode="after")
    def require_stable_identity(self) -> Self:
        if self.source_type == "mission":
            if self.source is not None or self.mission_ref is None or self.evidence_refs != (self.mission_ref,):
                raise ValueError("Mission exact read 未保留唯一 Mission 来源")
        elif self.source is None or self.mission_ref is not None:
            raise ValueError("Context exact read 缺少 Revision 来源")
        expected = stable_expansion_hash(
            "exact-evidence-read",
            self.session_id,
            self.candidate_id,
            self.index_id,
            self.entry_id,
            self.source_content_hash,
            self.evidence_refs,
            self.content,
        )
        if self.read_id != expected:
            raise ValueError("exact evidence read identity 与内容不一致")
        return self


def exact_read_matches_candidate(read: ExactEvidenceRead, candidate: RetrievalCandidate) -> bool:
    return read.candidate_id == candidate.candidate_id and all(
        getattr(read, field) == getattr(candidate, field)
        for field in (
            "index_id", "entry_id", "source_type", "source", "mission_ref", "source_content_hash", "index_schema_version",
            "segmenter_version", "projector_version",
        )
    )


class PortfolioIndexCatalog(_RetrievalModel):
    catalog_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    frontier_hash: str = Field(min_length=1)
    descriptors: tuple[RevisionIndexDescriptor, ...]
    segment_catalog: tuple[dict[str, Any], ...]
    mission_catalog: FrozenMissionSectionCatalog | None = None

    @classmethod
    def create(
        cls,
        *,
        frontier_hash: str,
        indexes: tuple[RevisionSemanticIndex, ...],
        mission_catalog: FrozenMissionSectionCatalog | None = None,
        max_descriptor_chars: int | None = ExpansionResourcePolicy().max_catalog_descriptor_chars,
    ) -> Self:
        descriptors = tuple(sorted((item.descriptor() for item in indexes), key=lambda item: item.index_id))
        segments = tuple(
            {
                "index_id": index.index_id,
                "segment_id": segment.segment_id,
                "ordinal": segment.ordinal,
                "descriptor": segment.descriptor,
                "content_hash": segment.content_hash,
            }
            for index in sorted(indexes, key=lambda item: item.index_id)
            for segment in index.segments
        )
        catalog_payload = (
            {"segments": segments, "mission": mission_catalog.model_dump(mode="json")}
            if mission_catalog else segments
        )
        size = len(json.dumps(catalog_payload, ensure_ascii=False, separators=(",", ":")))
        if exceeds_limit(size, max_descriptor_chars):
            raise PortfolioIndexCatalogOverflow("portfolio index catalog 超出预算，不能静默截断授权 index")
        identity_parts = (frontier_hash, tuple(item.index_id for item in descriptors), segments)
        if mission_catalog is not None:
            identity_parts += (mission_catalog.catalog_id,)
        identity = stable_expansion_hash("portfolio-index-catalog", *identity_parts)
        return cls(
            catalog_id=identity,
            frontier_hash=frontier_hash,
            descriptors=descriptors,
            segment_catalog=segments,
            mission_catalog=mission_catalog,
        )


class PlanningRetrievalSession(_RetrievalModel):
    session_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal["retrieval-session-v1", "retrieval-session-v2"] = "retrieval-session-v1"
    observation_hash: str = Field(min_length=1)
    frontier_hash: str = Field(min_length=1)
    catalog_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_index_ids: tuple[str, ...] = Field(min_length=1)
    source_context_ids: tuple[str, ...] = ()
    planner_version: str = Field(min_length=1, max_length=64)
    retrieval_version: str = Field(min_length=1, max_length=64)
    budget: RetrievalBudget
    frozen_resources: FrozenExpansionResources | None = None
    ledger: ExpansionUsageLedger | None = None
    usage: RetrievalUsage = Field(default_factory=RetrievalUsage)
    state: PlanningSessionState = "created"
    query_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    read_ids: tuple[str, ...] = ()
    queries: tuple[SemanticRetrievalQuery, ...] = ()
    candidates: tuple[RetrievalCandidate, ...] = ()
    query_hits: tuple[RetrievalQueryHit, ...] = ()
    query_coverage: tuple[RetrievalQueryCoverage, ...] = ()
    query_plan: tuple[SemanticRetrievalQuery, ...] = ()
    read_pages: tuple[CandidateDescriptorPage, ...] = ()
    read_page_cursor: int = Field(default=0, ge=0)
    selected_candidate_ids: tuple[str, ...] = ()
    model_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reads: tuple[ExactEvidenceRead, ...] = ()
    planned_payload: dict[str, Any] | None = None
    blocker_code: str | None = None
    blocker_summary: str | None = None
    blocker_stage: str | None = None
    blocker_boundary: str | None = None
    blocker_operation_id: str | None = None
    required_expansion_evaluation: bool = False

    @field_validator("authorized_index_ids", "source_context_ids", "query_ids", "candidate_ids", "read_ids", mode="before")
    @classmethod
    def normalize_identities(cls, values: Any) -> tuple[str, ...]:
        return tuple(sorted(set(values or ())))

    @model_validator(mode="after")
    def require_usage_within_budget(self) -> Self:
        if (self.schema_version == "retrieval-session-v2") != (self.frozen_resources is not None):
            raise ValueError("planning session schema 与冻结资源策略不一致")
        if (self.schema_version == "retrieval-session-v2") != (self.ledger is not None):
            raise ValueError("planning session schema 与 usage ledger 不一致")
        if self.frozen_resources is not None:
            policy = self.frozen_resources.policy
            expected = RetrievalBudget(
                max_queries=policy.max_queries,
                max_candidates=policy.max_unique_candidates,
                max_exact_reads=policy.max_exact_reads,
                max_model_calls=policy.max_planner_model_calls,
                max_tokens=policy.max_planner_tokens,
            )
            if self.budget != expected:
                raise ValueError("planning session budget 与冻结资源策略不一致")
            if self.ledger is not None and (
                self.usage.queries != self.ledger.total("query")
                or self.usage.candidates != self.ledger.total("unique_candidate")
                or self.usage.exact_reads != self.ledger.total("exact_read")
                or self.usage.model_calls != self.ledger.total("model_call")
                or self.usage.input_tokens != self.ledger.input_tokens
                or self.usage.output_tokens != self.ledger.output_tokens
                or self.usage.tokens != self.ledger.input_tokens + self.ledger.output_tokens
                or self.usage.unreported_model_calls != self.ledger.unreported_model_calls
            ):
                raise ValueError("planning session usage 与幂等 ledger 不一致")
        pairs = (
            (self.usage.queries, self.budget.max_queries),
            (self.usage.candidates, self.budget.max_candidates),
            (self.usage.exact_reads, self.budget.max_exact_reads),
            (self.usage.model_calls, self.budget.max_model_calls),
            (self.usage.tokens, self.budget.max_tokens),
        )
        if self.state not in {"blocked", "stale"} and any(exceeds_limit(used, maximum) for used, maximum in pairs):
            raise ValueError("planning retrieval session usage 超出冻结预算")
        if self.state in {"blocked", "stale"} and not self.blocker_code:
            raise ValueError("blocked/stale planning session 必须包含 blocker code")
        if self.state not in {"blocked", "stale"} and self.blocker_code is not None:
            raise ValueError("非阻断 planning session 不得携带 blocker code")
        if self.query_ids != tuple(sorted(item.query_id for item in self.queries)):
            raise ValueError("planning session query audit 与 identities 不一致")
        if self.candidate_ids != tuple(sorted(item.candidate_id for item in self.candidates)):
            raise ValueError("planning session candidate audit 与 identities 不一致")
        if self.schema_version == "retrieval-session-v2":
            if not self.source_context_ids:
                raise ValueError("新版 planning session 缺少 source Context identity")
            if any(item.identity_version != "entry-scoped-v2" for item in self.candidates):
                raise ValueError("新版 planning session 包含旧版 query-scoped candidate")
            if any(item.session_id != self.session_id for item in self.candidates):
                raise ValueError("新版 planning session 包含其它 session 的 candidate")
            if len(self.candidates) != len(self.candidate_ids) or self.usage.candidates != len(self.candidates):
                raise ValueError("新版 planning session 唯一候选核算不一致")
            if self.usage.exact_reads != len(self.reads):
                raise ValueError("新版 planning session 精读核算不一致")
            by_candidate = {item.candidate_id: item for item in self.candidates}
            if any(
                read.candidate_id not in by_candidate or not exact_read_matches_candidate(read, by_candidate[read.candidate_id])
                for read in self.reads
            ):
                raise ValueError("新版 planning session 精读 provenance 不一致")
            if len({item.hit_id for item in self.query_hits}) != len(self.query_hits):
                raise ValueError("新版 planning session query hit 重复")
            if any(item.query_id not in self.query_ids or item.candidate_id not in self.candidate_ids for item in self.query_hits):
                raise ValueError("新版 planning session query hit 来源不一致")
            if tuple(item.query_id for item in self.query_coverage) != tuple(item.query_id for item in self.queries):
                raise ValueError("新版 planning session query coverage 不完整")
            if self.queries != self.query_plan[:len(self.queries)]:
                raise ValueError("新版 planning session 已执行 queries 与冻结查询计划不一致")
            if not set(self.selected_candidate_ids).issubset(self.candidate_ids):
                raise ValueError("新版 planning session 选取了未召回 candidate")
            if self.read_page_cursor > len(self.read_pages):
                raise ValueError("新版 planning session read page cursor 越界")
            if tuple(page.ordinal for page in self.read_pages) != tuple(range(len(self.read_pages))):
                raise ValueError("新版 planning session read pages 顺序不一致")
            if any(page.page_id != stable_expansion_hash(
                "candidate-descriptor-page", self.session_id, page.ordinal, page.candidate_ids
            ) for page in self.read_pages):
                raise ValueError("新版 planning session read page identity 不一致")
            if any(not set(page.candidate_ids).issubset(self.candidate_ids) for page in self.read_pages):
                raise ValueError("新版 planning session read page 超出冻结候选")
            if self.read_pages and tuple(
                candidate_id for page in self.read_pages for candidate_id in page.candidate_ids
            ) != tuple(item.candidate_id for item in self.candidates if item.entry_kind in {"semantic_unit", "mission_section"}):
                raise ValueError("新版 planning session read pages 未完整覆盖冻结可精读 candidates")
        if self.read_ids != tuple(sorted(item.read_id for item in self.reads)):
            raise ValueError("planning session exact-read audit 与 identities 不一致")
        if any(item.session_id != self.session_id for item in self.reads):
            raise ValueError("planning session 包含其它 session 的 exact read")
        if self.state == "planned" and self.planned_payload is None:
            raise ValueError("planned retrieval session 必须持久化 planner output")
        return self

    @classmethod
    def create(
        cls,
        *,
        observation_hash: str,
        frontier_hash: str,
        catalog: PortfolioIndexCatalog,
        planner_version: str,
        retrieval_version: str,
        budget: RetrievalBudget,
        frozen_resources: FrozenExpansionResources | None = None,
    ) -> Self:
        index_ids = tuple(item.index_id for item in catalog.descriptors) + ((catalog.mission_catalog.catalog_id,) if catalog.mission_catalog else ())
        identity_parts: tuple[Any, ...] = (
            observation_hash,
            frontier_hash,
            catalog.catalog_id,
            index_ids,
            planner_version,
            retrieval_version,
            budget.model_dump(mode="json"),
        )
        if frozen_resources is not None:
            identity_parts += ("retrieval-session-v2", frozen_resources.model_dump(mode="json"))
        identity = stable_expansion_hash("planning-retrieval-session", *identity_parts)
        return cls(
            session_id=identity,
            schema_version="retrieval-session-v2" if frozen_resources is not None else "retrieval-session-v1",
            observation_hash=observation_hash,
            frontier_hash=frontier_hash,
            catalog_id=catalog.catalog_id,
            authorized_index_ids=index_ids,
            source_context_ids=tuple(sorted({item.source.context_id for item in catalog.descriptors})),
            planner_version=planner_version,
            retrieval_version=retrieval_version,
            budget=budget,
            frozen_resources=frozen_resources,
            required_expansion_evaluation=frozen_resources is not None,
            ledger=(
                ExpansionUsageLedger(ledger_id=stable_expansion_hash("expansion-usage-ledger", identity))
                if frozen_resources is not None else None
            ),
        )


class AuthorizedSemanticRetriever:
    VERSION = "authorized-lexical-retriever-v1"

    def __init__(self, mission_catalog: FrozenMissionSectionCatalog | None = None) -> None:
        self._mission_catalog = mission_catalog

    def retrieve(
        self,
        session: PlanningRetrievalSession,
        query: SemanticRetrievalQuery,
        indexes: tuple[RevisionSemanticIndex, ...],
    ) -> tuple[RetrievalCandidate, ...]:
        return self.retrieve_page(session, query, indexes)[0]

    def retrieve_page(
        self,
        session: PlanningRetrievalSession,
        query: SemanticRetrievalQuery,
        indexes: tuple[RevisionSemanticIndex, ...],
    ) -> tuple[tuple[RetrievalCandidate, ...], RetrievalQueryCoverage]:
        if session.state in {"planned", "blocked", "stale"}:
            raise ValueError("planning retrieval session 已终止")
        authorized = set(session.authorized_index_ids)
        requested = set(query.index_ids) or authorized
        if not requested.issubset(authorized):
            raise ValueError("retrieval query 请求了 session scope 之外的 index")
        if exceeds_limit(session.usage.queries + 1, session.budget.max_queries):
            raise ValueError("retrieval query budget exhausted")
        capacity = remaining_capacity(session.budget.max_candidates, session.usage.candidates)
        remaining = capacity
        query_terms = self._terms(query.text)
        ranked = []
        for index in indexes:
            if index.index_id not in requested:
                continue
            if "segment" in query.kinds:
                for segment in index.segments:
                    ranked.append((self._score(segment.descriptor, query_terms), index.index_id, "segment", segment.segment_id, segment.descriptor, index, None))
            if "semantic_unit" in query.kinds:
                for unit in index.semantic_units:
                    ranked.append((self._score(unit.statement, query_terms), index.index_id, "semantic_unit", unit.unit_id, unit.statement, index, None))
        mission = self._mission_catalog
        if session.schema_version == "retrieval-session-v2" and mission is not None and mission.catalog_id in requested and "mission_section" in query.kinds:
            for item, descriptor in zip(mission.entries, mission.descriptors(), strict=True):
                entry_id = stable_expansion_hash("mission-retrieval-entry", item.ref.model_dump(mode="json"))
                text = str(descriptor["descriptor"])
                ranked.append((self._score(text, query_terms), mission.catalog_id, "mission_section", entry_id, text, None, item))
        ranked.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
        selected = tuple((rank, *item) for rank, item in enumerate(ranked[:query.limit], start=1))
        if session.schema_version == "retrieval-session-v1":
            if remaining is not None and remaining <= 0:
                raise ValueError("retrieval candidate budget exhausted")
            selected = selected[:remaining]
        else:
            known = set(session.candidate_ids)
            bounded = []
            for rank, score, index_id, kind, entry_id, descriptor, index, mission_item in selected:
                digest = mission_item.ref.content_hash if mission_item else index.source_content_hash
                identity = stable_expansion_hash("retrieval-candidate-v2", index_id, kind, entry_id, digest)
                if identity in known or remaining is None or remaining > 0:
                    bounded.append((rank, score, index_id, kind, entry_id, descriptor, index, mission_item))
                    if identity not in known:
                        remaining = remaining_capacity(remaining, 1)
            selected = bounded
        candidates = tuple(
            RetrievalCandidate(
                candidate_id=(
                    stable_expansion_hash("retrieval-candidate-v2", index_id, kind, entry_id, mission_item.ref.content_hash if mission_item else index.source_content_hash)
                    if session.schema_version == "retrieval-session-v2"
                    else stable_expansion_hash("retrieval-candidate", query.query_id, index_id, kind, entry_id)
                ),
                identity_version="entry-scoped-v2" if session.schema_version == "retrieval-session-v2" else "query-scoped-v1",
                session_id=session.session_id if session.schema_version == "retrieval-session-v2" else None,
                query_id=query.query_id,
                index_id=index_id,
                entry_id=entry_id,
                entry_kind=kind,
                source_type="mission" if mission_item else "context",
                source=index.source if index else None,
                mission_ref=mission_item.ref if mission_item else None,
                source_content_hash=mission_item.ref.content_hash if mission_item else index.source_content_hash,
                index_schema_version=index.index_schema_version if index else "mission-section-v2",
                segmenter_version=index.segmenter_version if index else "mission-section-v2",
                projector_version=index.projector_version if index else "mission-section-v2",
                descriptor=descriptor,
                rank=rank,
                score=score,
                reasons=("normalized lexical overlap",) if score > 0 else ("authorized catalog fallback",),
            )
            for rank, score, index_id, kind, entry_id, descriptor, index, mission_item in selected
        )
        coverage = RetrievalQueryCoverage(
            query_id=query.query_id,
            requested=query.limit,
            allocated=query.limit if capacity is None else min(query.limit, capacity),
            returned=len(candidates),
            deduplicated=sum(item.candidate_id in session.candidate_ids for item in candidates),
            unvisited=len(ranked) - len(candidates),
        )
        return candidates, coverage

    @staticmethod
    def _score(descriptor: str, query_terms: frozenset[str]) -> float:
        terms = AuthorizedSemanticRetriever._terms(descriptor)
        union = query_terms | terms
        return len(query_terms & terms) / len(union) if union else 0.0

    @staticmethod
    def _terms(value: str) -> frozenset[str]:
        return frozenset(re.findall(r"[\w\u4e00-\u9fff]+", value.casefold()))

    def read(
        self,
        session: PlanningRetrievalSession,
        candidate: RetrievalCandidate,
        indexes: tuple[RevisionSemanticIndex, ...],
    ) -> ExactEvidenceRead:
        if candidate.index_id not in session.authorized_index_ids:
            raise ValueError("retrieval candidate 超出 session scope")
        if candidate.candidate_id not in session.candidate_ids:
            raise ValueError("retrieval candidate 未在同一 session 中返回")
        if session.schema_version == "retrieval-session-v2":
            stored = next(item for item in session.candidates if item.candidate_id == candidate.candidate_id)
            if candidate.session_id != session.session_id or any(
                getattr(candidate, field) != getattr(stored, field)
                for field in (
                    "identity_version", "index_id", "entry_id", "entry_kind", "source_type", "source", "mission_ref", "source_content_hash",
                    "index_schema_version", "segmenter_version", "projector_version", "descriptor",
                )
            ):
                raise ValueError("retrieval candidate 与冻结 session provenance 不一致")
            if not any(
                hit.query_id == candidate.query_id and hit.candidate_id == candidate.candidate_id
                for hit in session.query_hits
            ):
                raise ValueError("retrieval candidate query hit 未在同一 session 中返回")
        if exceeds_limit(session.usage.exact_reads + 1, session.budget.max_exact_reads):
            raise ValueError("exact evidence read budget exhausted")
        if candidate.source_type == "mission":
            return self._read_mission(session, candidate)
        index = next((item for item in indexes if item.index_id == candidate.index_id), None)
        if index is None or index.source != candidate.source or index.source_content_hash != candidate.source_content_hash:
            raise ValueError("retrieval candidate 的冻结 index 不可用")
        if (
            candidate.index_schema_version != index.index_schema_version
            or candidate.segmenter_version != index.segmenter_version
            or candidate.projector_version != index.projector_version
        ):
            raise ValueError("retrieval candidate 的 index version provenance 不一致")
        if candidate.entry_kind == "segment":
            segment = next((item for item in index.segments if item.segment_id == candidate.entry_id), None)
            if segment is None:
                raise ValueError("retrieval segment identity 不存在")
            selected = tuple(item for item in index.messages if item.message_id in set(segment.message_ids))
            refs = tuple(NamespacedMessageRef(source=index.source, message_id=item.message_id) for item in selected)
            content: Any = tuple(item.model_dump(mode="json") for item in selected)
        else:
            unit = next((item for item in index.semantic_units if item.unit_id == candidate.entry_id), None)
            if unit is None:
                raise ValueError("retrieval semantic unit identity 不存在")
            refs = unit.evidence_refs
            content = unit.model_dump(mode="json")
        payload = (
            session.session_id,
            candidate.candidate_id,
            index.index_id,
            candidate.entry_id,
            index.source_content_hash,
            refs,
            content,
        )
        return ExactEvidenceRead(
            read_id=stable_expansion_hash("exact-evidence-read", *payload),
            session_id=session.session_id,
            candidate_id=candidate.candidate_id,
            index_id=index.index_id,
            entry_id=candidate.entry_id,
            source=index.source,
            source_content_hash=index.source_content_hash,
            index_schema_version=index.index_schema_version,
            segmenter_version=index.segmenter_version,
            projector_version=index.projector_version,
            evidence_refs=refs,
            content=content,
        )

    def _read_mission(self, session: PlanningRetrievalSession, candidate: RetrievalCandidate) -> ExactEvidenceRead:
        catalog = self._mission_catalog
        if catalog is None or candidate.index_id != catalog.catalog_id or candidate.mission_ref is None:
            raise ValueError("Mission retrieval candidate 的冻结 catalog 不可用")
        item = catalog.resolve(candidate.mission_ref)
        if candidate.entry_id != stable_expansion_hash("mission-retrieval-entry", item.ref.model_dump(mode="json")):
            raise ValueError("Mission retrieval candidate section identity 不一致")
        refs = (item.ref,)
        payload = (session.session_id, candidate.candidate_id, candidate.index_id, candidate.entry_id, item.ref.content_hash, refs, item.content)
        return ExactEvidenceRead(
            read_id=stable_expansion_hash("exact-evidence-read", *payload),
            session_id=session.session_id,
            candidate_id=candidate.candidate_id,
            index_id=candidate.index_id,
            entry_id=candidate.entry_id,
            source_type="mission",
            mission_ref=item.ref,
            source_content_hash=item.ref.content_hash,
            index_schema_version="mission-section-v2",
            segmenter_version="mission-section-v2",
            projector_version="mission-section-v2",
            evidence_refs=refs,
            content=item.content,
        )


class PlanningRetrievalSessionController:
    @staticmethod
    def record_read_pages(
        session: PlanningRetrievalSession,
        pages: tuple[CandidateDescriptorPage, ...],
    ) -> PlanningRetrievalSession:
        if session.schema_version != "retrieval-session-v2":
            raise ValueError("planning read pages 需要新版 session")
        if session.read_pages:
            if session.read_pages != pages:
                raise ValueError("planning read pages 已冻结，不能重新分页")
            return session
        return PlanningRetrievalSessionController._updated(session, {"read_pages": pages})

    @staticmethod
    def record_query_plan(
        session: PlanningRetrievalSession,
        queries: tuple[SemanticRetrievalQuery, ...],
    ) -> PlanningRetrievalSession:
        if session.schema_version != "retrieval-session-v2" or session.query_plan:
            raise ValueError("planning query plan 已冻结或 session 版本不支持")
        return PlanningRetrievalSessionController._updated(session, {"query_plan": queries})

    @staticmethod
    def record_page_selection(
        session: PlanningRetrievalSession,
        *,
        page_ordinal: int,
        page_candidate_ids: tuple[str, ...],
        selected_ids: tuple[str, ...],
    ) -> PlanningRetrievalSession:
        if session.schema_version != "retrieval-session-v2" or page_ordinal != session.read_page_cursor:
            raise ValueError("planning read page cursor 不一致")
        if not set(selected_ids).issubset(page_candidate_ids) or not set(page_candidate_ids).issubset(session.candidate_ids):
            raise ValueError("planning read selection 超出当前 candidate page")
        return PlanningRetrievalSessionController._updated(
            session,
            {
                "read_page_cursor": page_ordinal + 1,
                "selected_candidate_ids": tuple(sorted(set((*session.selected_candidate_ids, *selected_ids)))),
            },
        )

    def record_query(
        self,
        session: PlanningRetrievalSession,
        query: SemanticRetrievalQuery,
        candidates: tuple[RetrievalCandidate, ...],
        coverage: RetrievalQueryCoverage | None = None,
    ) -> PlanningRetrievalSession:
        if any(item.query_id != query.query_id for item in candidates):
            raise ValueError("retrieval candidates 与 query identity 不一致")
        if any(item.index_id not in session.authorized_index_ids for item in candidates):
            raise ValueError("retrieval candidates 超出 session scope")
        if session.schema_version == "retrieval-session-v2" and any(item.session_id != session.session_id for item in candidates):
            raise ValueError("retrieval candidates 来自其它 planning session")
        if query.query_id in session.query_ids:
            prior = tuple(item for item in session.query_hits if item.query_id == query.query_id)
            if session.schema_version == "retrieval-session-v2" and prior == tuple(RetrievalQueryHit.from_candidate(item) for item in candidates):
                return session
            raise ValueError("retrieval query identity 已使用")
        if session.schema_version == "retrieval-session-v2":
            if coverage is None or coverage.query_id != query.query_id or coverage.returned != len(candidates):
                raise ValueError("新版 planning query 必须提供同一查询的 coverage")
            known = set(session.candidate_ids)
            fresh = tuple(item for item in candidates if item.candidate_id not in known)
            hits = tuple(RetrievalQueryHit.from_candidate(item) for item in candidates)
            ledger = session.ledger.record(ExpansionUsageCharge(operation_id=f"query:{query.query_id}", kind="query"))
            for item in fresh:
                ledger = ledger.record(ExpansionUsageCharge(operation_id=f"candidate:{item.candidate_id}", kind="unique_candidate"))
            usage = session.usage.model_copy(update={"queries": ledger.total("query"), "candidates": ledger.total("unique_candidate")})
            return self._updated(
                session,
                {
                    "usage": usage,
                    "state": "queried",
                    "query_ids": (*session.query_ids, query.query_id),
                    "candidate_ids": (*session.candidate_ids, *(item.candidate_id for item in fresh)),
                    "queries": (*session.queries, query),
                    "candidates": (*session.candidates, *fresh),
                    "query_hits": (*session.query_hits, *hits),
                    "query_coverage": (*session.query_coverage, coverage),
                    "ledger": ledger,
                },
            )
        usage = session.usage.model_copy(
            update={
                "queries": session.usage.queries + 1,
                "candidates": session.usage.candidates + len(candidates),
            }
        )
        return self._updated(
            session,
            {
                "usage": usage,
                "state": "queried",
                "query_ids": (*session.query_ids, query.query_id),
                "candidate_ids": (*session.candidate_ids, *(item.candidate_id for item in candidates)),
                "queries": (*session.queries, query),
                "candidates": (*session.candidates, *candidates),
            },
        )

    def record_reads(
        self,
        session: PlanningRetrievalSession,
        reads: tuple[ExactEvidenceRead, ...],
    ) -> PlanningRetrievalSession:
        if any(item.session_id != session.session_id for item in reads):
            raise ValueError("exact reads 来自其它 planning session")
        if any(item.candidate_id not in session.candidate_ids for item in reads):
            raise ValueError("exact read 未绑定本 session 返回的 candidate")
        if session.schema_version == "retrieval-session-v2":
            by_candidate = {item.candidate_id: item for item in session.candidates}
            if any(not exact_read_matches_candidate(read, by_candidate[read.candidate_id]) for read in reads):
                raise ValueError("exact read 与冻结 candidate provenance 不一致")
            ledger = session.ledger
            fresh = tuple(item for item in reads if item.read_id not in session.read_ids)
            for item in fresh:
                ledger = ledger.record(ExpansionUsageCharge(operation_id=f"read:{item.read_id}", kind="exact_read"))
            usage = session.usage.model_copy(update={"exact_reads": ledger.total("exact_read")})
            return self._updated(
                session,
                {
                    "usage": usage,
                    "ledger": ledger,
                    "state": "evidence_read",
                    "read_ids": (*session.read_ids, *(item.read_id for item in fresh)),
                    "reads": (*session.reads, *fresh),
                },
            )
        usage = session.usage.model_copy(update={"exact_reads": session.usage.exact_reads + len(reads)})
        return self._updated(
            session,
            {
                "usage": usage,
                "state": "evidence_read",
                "read_ids": (*session.read_ids, *(item.read_id for item in reads)),
                "reads": (*session.reads, *reads),
            },
        )

    @staticmethod
    def record_model_usage(
        session: PlanningRetrievalSession,
        *,
        model_calls: int,
        tokens: int,
        operation_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        result_payload: dict[str, Any] | None = None,
        result_operation_id: str | None = None,
        usage_reported: bool = True,
        stage: str | None = None,
    ) -> PlanningRetrievalSession:
        if session.schema_version == "retrieval-session-v2":
            if operation_id is None or input_tokens is None or output_tokens is None:
                raise ValueError("新版 planning model usage 必须提供 operation identity 与真实分项用量")
            ledger = session.ledger.record(
                ExpansionUsageCharge(
                    operation_id=f"model:{operation_id}",
                    kind="model_call",
                    units=model_calls,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    usage_reported=usage_reported,
                )
            )
            usage = session.usage.model_copy(
                update={
                    "model_calls": ledger.total("model_call"),
                    "tokens": ledger.input_tokens + ledger.output_tokens,
                    "input_tokens": ledger.input_tokens,
                    "output_tokens": ledger.output_tokens,
                    "unreported_model_calls": ledger.unreported_model_calls,
                }
            )
            over_budget = exceeds_limit(usage.model_calls, session.budget.max_model_calls) or exceeds_limit(usage.tokens, session.budget.max_tokens)
            results = dict(session.model_results)
            if result_payload is not None:
                result_key = result_operation_id or operation_id
                previous = results.get(result_key)
                if previous is not None and previous != result_payload:
                    raise ValueError("planning model result operation identity 冲突")
                results[result_key] = result_payload
            values = {"ledger": ledger, "usage": usage, "model_results": results}
            if over_budget:
                boundary = "max_planner_model_calls" if exceeds_limit(usage.model_calls, session.budget.max_model_calls) else "max_planner_tokens"
                values.update({
                    "state": "blocked",
                    "blocker_code": "expansion_policy_limit",
                    "blocker_summary": "规划模型调用超过冻结上限" if boundary == "max_planner_model_calls" else "规划 Token 超过冻结上限",
                    "blocker_stage": stage,
                    "blocker_boundary": boundary,
                    "blocker_operation_id": operation_id,
                })
            if not usage_reported:
                values.update({"state": "blocked", "blocker_code": "model_usage_unavailable", "blocker_summary": "provider 未报告本次模型调用的真实 Token 用量", "blocker_stage": stage, "blocker_boundary": "provider_usage", "blocker_operation_id": operation_id})
            return PlanningRetrievalSessionController._updated(session, values)
        usage = session.usage.model_copy(
            update={
                "model_calls": session.usage.model_calls + model_calls,
                "tokens": session.usage.tokens + tokens,
            }
        )
        over_budget = (
            exceeds_limit(usage.model_calls, session.budget.max_model_calls)
            or exceeds_limit(usage.tokens, session.budget.max_tokens)
        )
        values: dict[str, Any] = {"usage": usage}
        if over_budget:
            values.update({"state": "blocked", "blocker_code": "retrieval_budget_exhausted"})
        return PlanningRetrievalSessionController._updated(session, values)

    @staticmethod
    def has_model_capacity(session: PlanningRetrievalSession) -> bool:
        return (
            not exceeds_limit(session.usage.model_calls, session.budget.max_model_calls, inclusive=True)
            and not exceeds_limit(session.usage.tokens, session.budget.max_tokens, inclusive=True)
        )

    @staticmethod
    def finish(
        session: PlanningRetrievalSession,
        planned_payload: dict[str, Any],
    ) -> PlanningRetrievalSession:
        if session.state not in {"created", "queried", "evidence_read"}:
            raise ValueError("planning retrieval session 无法完成")
        return PlanningRetrievalSessionController._updated(
            session,
            {"state": "planned", "planned_payload": planned_payload},
        )

    @staticmethod
    def block(
        session: PlanningRetrievalSession,
        code: str,
        *,
        stale: bool = False,
        summary: str | None = None,
        stage: str | None = None,
        boundary: str | None = None,
        operation_id: str | None = None,
    ) -> PlanningRetrievalSession:
        return PlanningRetrievalSessionController._updated(
            session,
            {
                "state": "stale" if stale else "blocked",
                "blocker_code": code,
                "blocker_summary": summary,
                "blocker_stage": stage,
                "blocker_boundary": boundary,
                "blocker_operation_id": operation_id,
            },
        )

    @staticmethod
    def _updated(
        session: PlanningRetrievalSession,
        values: dict[str, Any],
    ) -> PlanningRetrievalSession:
        payload = session.model_dump(mode="python")
        payload.update(values)
        return PlanningRetrievalSession.model_validate(payload)
