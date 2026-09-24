r"""本文件对外提供 RetrievalBackedCognitiveAdvisor 及其分步 worker schemas。

输入为冻结 mission/workspace facts、PortfolioIndexCatalog、授权 RevisionSemanticIndex、planning session 与只提供结构化调用的模型端口；
输出为 WorkContextDraft 候选、同 session 已验证的 semantic manifests、exact reads 和终态 session。具体工作流为模型先提交有界查询，
服务端在授权索引内召回候选，模型再选择需要精读的 semantic-unit candidates，服务端精确读取并核算预算，最终模型只能引用已精读
unit identity 生成工作候选；任一步不完整均显式阻断。示例：`result = await advisor.plan(payload, session, indexes)`。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from focus.runtime.runs.usage import ModelUsage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    ContextSemanticManifest,
    WorkContextDraft,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_index import (
    RevisionSemanticIndex,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
    AuthorizedSemanticRetriever,
    ExactEvidenceRead,
    PlanningRetrievalSession,
    PlanningRetrievalSessionController,
    RetrievalCandidate,
    SemanticRetrievalQuery,
)


class _PlannerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievalQueryDraft(_PlannerModel):
    text: str = Field(min_length=1, max_length=4000)
    index_ids: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ("semantic_unit", "segment")
    limit: int = Field(default=16, ge=1, le=128)


class PlannerQueryProposal(_PlannerModel):
    rationale: str = Field(min_length=1, max_length=4000)
    queries: tuple[RetrievalQueryDraft, ...] = Field(min_length=1, max_length=8)


class PlannerReadProposal(_PlannerModel):
    rationale: str = Field(min_length=1, max_length=4000)
    candidate_ids: tuple[str, ...] = Field(min_length=1, max_length=64)


class LaneAdviceProposal(_PlannerModel):
    rationale: str = Field(min_length=1, max_length=4000)
    work_specs: tuple[WorkContextDraft, ...]


class RetrievalBackedPlanningResult(_PlannerModel):
    proposal: LaneAdviceProposal | None = None
    session: PlanningRetrievalSession
    candidates: tuple[RetrievalCandidate, ...] = ()
    reads: tuple[ExactEvidenceRead, ...] = ()
    manifests: tuple[ContextSemanticManifest, ...] = ()
    blocker_code: str | None = None
    blocker_summary: str | None = None

    @model_validator(mode="after")
    def require_result_shape(self):
        blocked = self.blocker_code is not None or self.blocker_summary is not None
        if blocked and not (self.blocker_code and self.blocker_summary):
            raise ValueError("retrieval planning blocker 必须包含 code 与 summary")
        if (self.proposal is None) != blocked:
            raise ValueError("retrieval planning result 必须恰好包含 proposal 或 blocker")
        return self


class StructuredModelPort(Protocol):
    async def invoke(self, schema, system: str, payload: dict[str, Any]): ...


class RetrievalBackedCognitiveAdvisor:
    VERSION = "retrieval-backed-cognitive-advisor-v1"

    def __init__(
        self,
        model: StructuredModelPort,
        retriever: AuthorizedSemanticRetriever | None = None,
        controller: PlanningRetrievalSessionController | None = None,
        checkpoint: Callable[[PlanningRetrievalSession], Awaitable[None]] | None = None,
    ) -> None:
        self._model = model
        self._retriever = retriever or AuthorizedSemanticRetriever()
        self._controller = controller or PlanningRetrievalSessionController()
        self._checkpoint = checkpoint

    async def plan(
        self,
        payload: dict[str, Any],
        session: PlanningRetrievalSession,
        indexes: tuple[RevisionSemanticIndex, ...],
    ) -> RetrievalBackedPlanningResult:
        try:
            if session.state in {"blocked", "stale"}:
                return RetrievalBackedPlanningResult(
                    session=session,
                    blocker_code=session.blocker_code or "retrieval_planning_failed",
                    blocker_summary="planning retrieval session 已阻断",
                )
            if session.state == "planned":
                proposal = LaneAdviceProposal.model_validate(session.planned_payload)
                selected = self._selected_from_reads(session)
                return RetrievalBackedPlanningResult(
                    proposal=proposal,
                    session=session,
                    candidates=session.candidates,
                    reads=session.reads,
                    manifests=self._manifests(indexes, selected),
                )
            candidates = list(session.candidates)
            if session.state == "created":
                query_proposal, session = await self._invoke(
                    session,
                    PlannerQueryProposal,
                    self._query_authority(),
                    self._planning_input(payload),
                )
                if session.state == "blocked":
                    return await self._budget_blocked(session)
                for draft in query_proposal.queries:
                    query = SemanticRetrievalQuery.create(
                        text=draft.text,
                        index_ids=draft.index_ids,
                        kinds=tuple(draft.kinds),
                        limit=draft.limit,
                    )
                    returned = self._retriever.retrieve(session, query, indexes)
                    session = self._controller.record_query(session, query, returned)
                    candidates.extend(returned)
                await self._save(session)
            if not candidates:
                return self._blocked(session, "retrieval_empty", "semantic retrieval 没有返回可验证候选")
            if session.state == "queried":
                read_proposal, session = await self._invoke(
                    session,
                    PlannerReadProposal,
                    self._read_authority(),
                    {
                        **self._planning_input(payload),
                        "candidates": tuple(item.model_dump(mode="json") for item in candidates),
                    },
                )
                if session.state == "blocked":
                    return await self._budget_blocked(session)
                by_candidate = {item.candidate_id: item for item in candidates}
                selected_optional = tuple(by_candidate.get(candidate_id) for candidate_id in read_proposal.candidate_ids)
                if any(item is None for item in selected_optional):
                    return self._blocked(session, "planner_candidate_unknown", "Planner 请求了本 session 未返回的 candidate")
                selected = tuple(item for item in selected_optional if item is not None)
                if any(item.entry_kind != "semantic_unit" for item in selected):
                    return self._blocked(session, "planner_read_kind_invalid", "WorkSpec evidence 只能由 semantic-unit candidate 提升")
                reads = tuple(self._retriever.read(session, item, indexes) for item in selected)
                session = self._controller.record_reads(session, reads)
                await self._save(session)
            else:
                reads = session.reads
                selected = self._selected_from_reads(session)
            manifests = self._manifests(indexes, selected)
            proposal, session = await self._invoke(
                session,
                LaneAdviceProposal,
                self._work_authority(),
                {
                    **self._planning_input(payload),
                    "exact_reads": tuple(item.model_dump(mode="json") for item in reads),
                    "semantic_manifests": tuple(item.model_dump(mode="json") for item in manifests),
                    "allowed_candidate_unit_ids": tuple(item.entry_id for item in selected),
                },
            )
            if session.state == "blocked":
                return await self._budget_blocked(session)
            invented = {
                unit_id
                for draft in proposal.work_specs
                for requirement in draft.evidence_requirements
                for unit_id in requirement.candidate_unit_ids
                if unit_id not in {item.entry_id for item in selected}
            }
            if invented:
                return self._blocked(session, "planner_evidence_identity_unknown", "Planner 引用了未在同一 session 精读的 candidate identity")
            session = self._controller.finish(
                session,
                proposal.model_dump(mode="json"),
            )
            await self._save(session)
            return RetrievalBackedPlanningResult(
                proposal=proposal,
                session=session,
                candidates=tuple(candidates),
                reads=reads,
                manifests=manifests,
            )
        except ValueError as exc:
            code = "retrieval_budget_exhausted" if "budget" in str(exc).casefold() else "retrieval_planning_failed"
            return self._blocked(session, code, str(exc)[:1600])

    async def _save(self, session: PlanningRetrievalSession) -> None:
        if self._checkpoint is not None:
            await self._checkpoint(session)

    async def _invoke(self, session, schema, authority: str, payload: dict[str, Any]):
        if not self._controller.has_model_capacity(session):
            return None, self._controller.block(session, "retrieval_budget_exhausted")
        result = await self._model.invoke(schema, authority, payload)
        usage = getattr(self._model, "last_usage", ModelUsage(model_calls=1))
        calls = max(1, int(getattr(usage, "model_calls", 0)))
        tokens = int(getattr(usage, "input_tokens", 0)) + int(getattr(usage, "output_tokens", 0))
        updated = self._controller.record_model_usage(
            session,
            model_calls=calls,
            tokens=max(0, tokens),
        )
        await self._save(updated)
        return result, updated

    async def _budget_blocked(self, session: PlanningRetrievalSession) -> RetrievalBackedPlanningResult:
        await self._save(session)
        return RetrievalBackedPlanningResult(
            session=session,
            blocker_code="retrieval_budget_exhausted",
            blocker_summary="planning retrieval model/token budget exhausted",
        )

    @staticmethod
    def _selected_from_reads(
        session: PlanningRetrievalSession,
    ) -> tuple[RetrievalCandidate, ...]:
        read_candidates = {item.candidate_id for item in session.reads}
        return tuple(
            item
            for item in session.candidates
            if item.candidate_id in read_candidates
        )

    @staticmethod
    def _planning_input(payload: dict[str, Any]) -> dict[str, Any]:
        scope = dict(payload.get("scope") or {})
        derivation = dict(scope.get("derivation_input") or {})
        derivation.pop("semantic_index_ids", None)
        scope["derivation_input"] = derivation
        return {
            "mission": payload.get("mission"),
            "frontier_hash": payload.get("frontier_hash"),
            "workspace": payload.get("workspace"),
            "run_evidence": payload.get("run_evidence"),
            "assignments": payload.get("assignments") or scope.get("assignments") or (),
            "derivation": derivation,
        }

    def _blocked(
        self,
        session: PlanningRetrievalSession,
        code: str,
        summary: str,
    ) -> RetrievalBackedPlanningResult:
        terminal = self._controller.block(session, code)
        return RetrievalBackedPlanningResult(
            session=terminal,
            blocker_code=code,
            blocker_summary=summary,
        )

    @staticmethod
    def _manifests(
        indexes: tuple[RevisionSemanticIndex, ...],
        selected: tuple[RetrievalCandidate, ...],
    ) -> tuple[ContextSemanticManifest, ...]:
        selected_by_index: dict[str, set[str]] = {}
        for candidate in selected:
            selected_by_index.setdefault(candidate.index_id, set()).add(candidate.entry_id)
        return tuple(
            ContextSemanticManifest.create(
                source=index.source,
                source_content_hash=index.source_content_hash,
                projector_version=index.projector_version,
                role=index.context_role,
                active_objective=index.active_objective,
                units=tuple(
                    unit
                    for unit in index.semantic_units
                    if unit.unit_id in selected_by_index.get(index.index_id, set())
                ),
            )
            for index in indexes
            if index.index_id in selected_by_index
        )

    @staticmethod
    def _query_authority() -> str:
        return "你是无权 Retrieval Query Planner。只根据冻结 Mission、Portfolio index catalog、Run 与 Workspace facts 提交少量语义查询；不得生成 WorkSpec、evidence、执行指令或状态变更。查询只能选择输入 catalog 中的 index_id。"

    @staticmethod
    def _read_authority() -> str:
        return "你是无权 Evidence Read Selector。只从服务端返回的 candidates 中选择必须精读的 semantic_unit candidate_id；不得选择 segment、发明 identity、生成 WorkSpec 或执行指令。"

    @staticmethod
    def _work_authority() -> str:
        return "你是无权 Cognitive Work Planner。只使用本 planning session 已精确读取的 evidence 和 allowed_candidate_unit_ids 生成零个或多个 WorkContextDraft；Context evidence requirement 必须引用允许的 unit identity。按真实认知职责决定是否需要独立历史，不得按关键词/阈值套模板，不得创建 Context、修改状态、运行工具或扩大 scope。"
