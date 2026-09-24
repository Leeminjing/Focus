r"""本文件对外提供 PortfolioIndexCatalogOverflow、PortfolioIndexCatalog、SemanticRetrievalQuery、RetrievalCandidate、ExactEvidenceRead、
PlanningRetrievalSession、AuthorizedSemanticRetriever 与 PlanningRetrievalSessionController。

输入为冻结 RevisionSemanticIndex 集合、授权 index identities、结构化查询与硬预算；输出为有理由的有界候选、精确 evidence reads
及可持久化的 planning session。具体工作流为先构建不丢 index identity 的有界目录，再在授权 scope 内稳定排序 segment/unit，
按 candidate identity 精确读取冻结内容并原子核算 query/read/model/token 消耗；越权、stale、超预算或非法状态均显式失败。
示例：`candidates = retriever.retrieve(session, query, indexes)`。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_index import (
    IndexEntryKind,
    RevisionIndexDescriptor,
    RevisionSemanticIndex,
)
from backend.app.desktop.context_curation import EvidenceRef, NamespacedMessageRef
from backend.app.desktop.context_evolution import ContextRevisionRef

PlanningSessionState = Literal["created", "queried", "evidence_read", "planned", "blocked", "stale"]


class _RetrievalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PortfolioIndexCatalogOverflow(ValueError):
    pass


class RetrievalBudget(_RetrievalModel):
    max_queries: int = Field(ge=0)
    max_candidates: int = Field(ge=0)
    max_exact_reads: int = Field(ge=0)
    max_model_calls: int = Field(ge=0)
    max_tokens: int = Field(ge=0)


class RetrievalUsage(_RetrievalModel):
    queries: int = Field(default=0, ge=0)
    candidates: int = Field(default=0, ge=0)
    exact_reads: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)


class SemanticRetrievalQuery(_RetrievalModel):
    query_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=4000)
    index_ids: tuple[str, ...] = ()
    kinds: tuple[IndexEntryKind, ...] = ("semantic_unit", "segment")
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
        kinds: tuple[IndexEntryKind, ...] = ("semantic_unit", "segment"),
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
    query_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_kind: IndexEntryKind
    source: ContextRevisionRef
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
        expected = stable_expansion_hash(
            "retrieval-candidate",
            self.query_id,
            self.index_id,
            self.entry_kind,
            self.entry_id,
        )
        if self.candidate_id != expected:
            raise ValueError("retrieval candidate identity 与来源不一致")
        return self


class ExactEvidenceRead(_RetrievalModel):
    read_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: ContextRevisionRef
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_schema_version: str = Field(min_length=1, max_length=64)
    segmenter_version: str = Field(min_length=1, max_length=64)
    projector_version: str = Field(min_length=1, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...]
    content: Any

    @model_validator(mode="after")
    def require_stable_identity(self) -> Self:
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


class PortfolioIndexCatalog(_RetrievalModel):
    catalog_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    frontier_hash: str = Field(min_length=1)
    descriptors: tuple[RevisionIndexDescriptor, ...]
    segment_catalog: tuple[dict[str, Any], ...]

    @classmethod
    def create(
        cls,
        *,
        frontier_hash: str,
        indexes: tuple[RevisionSemanticIndex, ...],
        max_descriptor_chars: int = 64000,
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
        size = len(json.dumps(segments, ensure_ascii=False, separators=(",", ":")))
        if size > max_descriptor_chars:
            raise PortfolioIndexCatalogOverflow("portfolio index catalog 超出预算，不能静默截断授权 index")
        identity = stable_expansion_hash(
            "portfolio-index-catalog",
            frontier_hash,
            tuple(item.index_id for item in descriptors),
            segments,
        )
        return cls(
            catalog_id=identity,
            frontier_hash=frontier_hash,
            descriptors=descriptors,
            segment_catalog=segments,
        )


class PlanningRetrievalSession(_RetrievalModel):
    session_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_hash: str = Field(min_length=1)
    frontier_hash: str = Field(min_length=1)
    catalog_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_index_ids: tuple[str, ...] = Field(min_length=1)
    planner_version: str = Field(min_length=1, max_length=64)
    retrieval_version: str = Field(min_length=1, max_length=64)
    budget: RetrievalBudget
    usage: RetrievalUsage = Field(default_factory=RetrievalUsage)
    state: PlanningSessionState = "created"
    query_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    read_ids: tuple[str, ...] = ()
    queries: tuple[SemanticRetrievalQuery, ...] = ()
    candidates: tuple[RetrievalCandidate, ...] = ()
    reads: tuple[ExactEvidenceRead, ...] = ()
    planned_payload: dict[str, Any] | None = None
    blocker_code: str | None = None

    @field_validator("authorized_index_ids", "query_ids", "candidate_ids", "read_ids", mode="before")
    @classmethod
    def normalize_identities(cls, values: Any) -> tuple[str, ...]:
        return tuple(sorted(set(values or ())))

    @model_validator(mode="after")
    def require_usage_within_budget(self) -> Self:
        pairs = (
            (self.usage.queries, self.budget.max_queries),
            (self.usage.candidates, self.budget.max_candidates),
            (self.usage.exact_reads, self.budget.max_exact_reads),
            (self.usage.model_calls, self.budget.max_model_calls),
            (self.usage.tokens, self.budget.max_tokens),
        )
        if self.state not in {"blocked", "stale"} and any(used > maximum for used, maximum in pairs):
            raise ValueError("planning retrieval session usage 超出冻结预算")
        if self.state in {"blocked", "stale"} and not self.blocker_code:
            raise ValueError("blocked/stale planning session 必须包含 blocker code")
        if self.state not in {"blocked", "stale"} and self.blocker_code is not None:
            raise ValueError("非阻断 planning session 不得携带 blocker code")
        if self.query_ids != tuple(sorted(item.query_id for item in self.queries)):
            raise ValueError("planning session query audit 与 identities 不一致")
        if self.candidate_ids != tuple(sorted(item.candidate_id for item in self.candidates)):
            raise ValueError("planning session candidate audit 与 identities 不一致")
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
    ) -> Self:
        index_ids = tuple(item.index_id for item in catalog.descriptors)
        identity = stable_expansion_hash(
            "planning-retrieval-session",
            observation_hash,
            frontier_hash,
            catalog.catalog_id,
            index_ids,
            planner_version,
            retrieval_version,
            budget.model_dump(mode="json"),
        )
        return cls(
            session_id=identity,
            observation_hash=observation_hash,
            frontier_hash=frontier_hash,
            catalog_id=catalog.catalog_id,
            authorized_index_ids=index_ids,
            planner_version=planner_version,
            retrieval_version=retrieval_version,
            budget=budget,
        )


class AuthorizedSemanticRetriever:
    VERSION = "authorized-lexical-retriever-v1"

    def retrieve(
        self,
        session: PlanningRetrievalSession,
        query: SemanticRetrievalQuery,
        indexes: tuple[RevisionSemanticIndex, ...],
    ) -> tuple[RetrievalCandidate, ...]:
        if session.state in {"planned", "blocked", "stale"}:
            raise ValueError("planning retrieval session 已终止")
        authorized = set(session.authorized_index_ids)
        requested = set(query.index_ids) or authorized
        if not requested.issubset(authorized):
            raise ValueError("retrieval query 请求了 session scope 之外的 index")
        if session.usage.queries + 1 > session.budget.max_queries:
            raise ValueError("retrieval query budget exhausted")
        remaining = session.budget.max_candidates - session.usage.candidates
        if remaining <= 0:
            raise ValueError("retrieval candidate budget exhausted")
        query_terms = self._terms(query.text)
        ranked = []
        for index in indexes:
            if index.index_id not in requested:
                continue
            if "segment" in query.kinds:
                for segment in index.segments:
                    ranked.append(self._ranked(index, "segment", segment.segment_id, segment.descriptor, query_terms))
            if "semantic_unit" in query.kinds:
                for unit in index.semantic_units:
                    ranked.append(self._ranked(index, "semantic_unit", unit.unit_id, unit.statement, query_terms))
        ranked.sort(key=lambda item: (-item[0], item[1].index_id, item[2], item[3]))
        selected = ranked[: min(query.limit, remaining)]
        return tuple(
            RetrievalCandidate(
                candidate_id=stable_expansion_hash(
                    "retrieval-candidate",
                    query.query_id,
                    index.index_id,
                    kind,
                    entry_id,
                ),
                query_id=query.query_id,
                index_id=index.index_id,
                entry_id=entry_id,
                entry_kind=kind,
                source=index.source,
                source_content_hash=index.source_content_hash,
                index_schema_version=index.index_schema_version,
                segmenter_version=index.segmenter_version,
                projector_version=index.projector_version,
                descriptor=descriptor,
                rank=rank,
                score=score,
                reasons=("normalized lexical overlap",) if score > 0 else ("authorized catalog fallback",),
            )
            for rank, (score, index, kind, entry_id, descriptor) in enumerate(selected, start=1)
        )

    @staticmethod
    def _ranked(
        index: RevisionSemanticIndex,
        kind: IndexEntryKind,
        entry_id: str,
        descriptor: str,
        query_terms: frozenset[str],
    ) -> tuple[float, RevisionSemanticIndex, IndexEntryKind, str, str]:
        terms = AuthorizedSemanticRetriever._terms(descriptor)
        union = query_terms | terms
        score = len(query_terms & terms) / len(union) if union else 0.0
        return score, index, kind, entry_id, descriptor

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
        if session.usage.exact_reads + 1 > session.budget.max_exact_reads:
            raise ValueError("exact evidence read budget exhausted")
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


class PlanningRetrievalSessionController:
    def record_query(
        self,
        session: PlanningRetrievalSession,
        query: SemanticRetrievalQuery,
        candidates: tuple[RetrievalCandidate, ...],
    ) -> PlanningRetrievalSession:
        if any(item.query_id != query.query_id for item in candidates):
            raise ValueError("retrieval candidates 与 query identity 不一致")
        if any(item.index_id not in session.authorized_index_ids for item in candidates):
            raise ValueError("retrieval candidates 超出 session scope")
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
    ) -> PlanningRetrievalSession:
        usage = session.usage.model_copy(
            update={
                "model_calls": session.usage.model_calls + model_calls,
                "tokens": session.usage.tokens + tokens,
            }
        )
        over_budget = (
            usage.model_calls > session.budget.max_model_calls
            or usage.tokens > session.budget.max_tokens
        )
        values: dict[str, Any] = {"usage": usage}
        if over_budget:
            values.update({"state": "blocked", "blocker_code": "retrieval_budget_exhausted"})
        return PlanningRetrievalSessionController._updated(session, values)

    @staticmethod
    def has_model_capacity(session: PlanningRetrievalSession) -> bool:
        return (
            session.usage.model_calls < session.budget.max_model_calls
            and session.usage.tokens < session.budget.max_tokens
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
    def block(session: PlanningRetrievalSession, code: str, *, stale: bool = False) -> PlanningRetrievalSession:
        return PlanningRetrievalSessionController._updated(
            session,
            {"state": "stale" if stale else "blocked", "blocker_code": code},
        )

    @staticmethod
    def _updated(
        session: PlanningRetrievalSession,
        values: dict[str, Any],
    ) -> PlanningRetrievalSession:
        payload = session.model_dump(mode="python")
        payload.update(values)
        return PlanningRetrievalSession.model_validate(payload)
