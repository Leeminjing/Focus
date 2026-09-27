r"""本文件对外提供 RetrievalBackedCognitiveAdvisor 及其分步 worker schemas。

输入为冻结 mission/workspace facts、PortfolioIndexCatalog、授权 RevisionSemanticIndex、planning session 与只提供结构化调用的模型端口；
输出为 WorkContextDraft 候选、同 session 已验证的 semantic manifests、exact reads 和终态 session。具体工作流为模型先提交经整体容量检查的查询，
服务端在授权索引内召回去重候选，模型按持久分页选择 semantic-unit candidates，服务端精读并以实际用量记账；失败尝试可从稳定操作身份恢复，
最终模型按单请求窗口分页处理所有精读证据，只能引用所在批次已精读 unit identity 生成工作候选，同一职责跨页归并且保留全部引用，资源与请求窗口阻断保留因果代码。示例：`result = await advisor.plan(payload, session, indexes)`。
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
from backend.app.desktop.agent_loop.context_expansion.candidate_paging import (
    CandidateDescriptorPager,
    ProviderRequestWindowError,
)
from backend.app.desktop.agent_loop.context_expansion.evidence_read_paging import EvidenceReadPager
from backend.app.desktop.agent_loop.context_expansion.query_admission import (
    QueryPlanAdmission,
    QueryPlanAdmissionError,
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
from backend.app.desktop.agent_loop.context_expansion.work_spec_pages import combine_page_work_specs


class _PlannerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievalQueryDraft(_PlannerModel):
    text: str = Field(min_length=1, max_length=4000)
    index_ids: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ("semantic_unit", "segment")
    limit: int = Field(default=16, ge=1, le=128)


class PlannerQueryProposal(_PlannerModel):
    rationale: str = Field(min_length=1, max_length=4000)
    queries: tuple[RetrievalQueryDraft, ...] = Field(min_length=1)


class PlannerReadProposal(_PlannerModel):
    rationale: str = Field(min_length=1, max_length=4000)
    candidate_ids: tuple[str, ...]


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
            if session.schema_version == "retrieval-session-v2":
                session = await self._run_v2_queries(payload, session, indexes)
            elif session.state == "created":
                query_proposal, session = await self._invoke(
                    session,
                    PlannerQueryProposal,
                    self._query_authority(),
                    self._planning_input(payload),
                    operation_id="query_plan",
                )
                if session.state == "blocked":
                    return await self._budget_blocked(session)
                queries = tuple(
                    SemanticRetrievalQuery.create(
                        text=draft.text,
                        index_ids=draft.index_ids,
                        kinds=tuple(draft.kinds),
                        limit=draft.limit,
                    )
                    for draft in query_proposal.queries
                )
                for query in queries:
                    returned = self._retriever.retrieve(session, query, indexes)
                    session = self._controller.record_query(session, query, returned)
                await self._save(session)
            if session.state == "blocked":
                return await self._budget_blocked(session)
            candidates = list(session.candidates)
            if not candidates:
                return self._blocked(session, "retrieval_empty", "semantic retrieval 没有返回可验证候选")
            if session.state == "queried":
                if session.schema_version == "retrieval-session-v2":
                    session = await self._select_v2_reads(payload, session, indexes)
                else:
                    session = await self._select_v1_reads(payload, session, indexes)
                if session.state == "blocked":
                    return await self._budget_blocked(session)
            reads = session.reads
            selected = self._selected_from_reads(session)
            manifests = self._manifests(indexes, selected)
            if session.schema_version == "retrieval-session-v2":
                proposal, session = await self._plan_v2_work(payload, session)
            else:
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
                    operation_id="work_spec",
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
        except QueryPlanAdmissionError as exc:
            blocked = self._blocked(session, exc.code, str(exc), stage="query_plan", boundary=exc.boundary, operation_id="query_plan")
            await self._save(blocked.session)
            return blocked
        except ProviderRequestWindowError as exc:
            blocked = self._blocked(session, "provider_request_window", str(exc), stage=exc.stage, boundary="max_request_input_tokens", operation_id=f"{exc.stage}_paging")
            await self._save(blocked.session)
            return blocked
        except ValueError as exc:
            if session.schema_version == "retrieval-session-v2":
                raise
            code = "retrieval_budget_exhausted" if "budget" in str(exc).casefold() else "retrieval_planning_failed"
            return self._blocked(session, code, "历史 planning session 无法继续检索")

    async def _run_v2_queries(
        self,
        payload: dict[str, Any],
        session: PlanningRetrievalSession,
        indexes: tuple[RevisionSemanticIndex, ...],
    ) -> PlanningRetrievalSession:
        if not session.query_plan:
            proposal, session = await self._invoke(
                session,
                PlannerQueryProposal,
                self._query_authority(),
                self._planning_input(payload),
                operation_id="query_plan",
            )
            if session.state == "blocked":
                return session
            queries = tuple(
                SemanticRetrievalQuery.create(
                    text=draft.text,
                    index_ids=draft.index_ids,
                    kinds=tuple(draft.kinds),
                    limit=draft.limit,
                )
                for draft in proposal.queries
            )
            try:
                admitted = QueryPlanAdmission().admit(session, queries)
            except QueryPlanAdmissionError as exc:
                blocked = self._controller.block(session, exc.code, summary=str(exc), stage="query_plan", boundary=exc.boundary, operation_id="query_plan")
                await self._save(blocked)
                return blocked
            session = self._controller.record_query_plan(session, admitted)
            await self._save(session)
        for query in session.query_plan[len(session.queries):]:
            candidates, coverage = self._retriever.retrieve_page(session, query, indexes)
            session = self._controller.record_query(session, query, candidates, coverage)
            await self._save(session)
        return session

    async def _select_v1_reads(
        self,
        payload: dict[str, Any],
        session: PlanningRetrievalSession,
        indexes: tuple[RevisionSemanticIndex, ...],
    ) -> PlanningRetrievalSession:
        candidates = session.candidates
        proposal, session = await self._invoke(
            session,
            PlannerReadProposal,
            self._read_authority(),
            {
                **self._planning_input(payload),
                "candidates": tuple(item.model_dump(mode="json") for item in candidates),
            },
            operation_id="read_selection",
        )
        if session.state == "blocked":
            return session
        by_candidate = {item.candidate_id: item for item in candidates}
        selected = tuple(by_candidate.get(candidate_id) for candidate_id in proposal.candidate_ids)
        if any(item is None for item in selected):
            return self._controller.block(session, "planner_candidate_unknown")
        if any(item.entry_kind != "semantic_unit" for item in selected):
            return self._controller.block(session, "planner_read_kind_invalid")
        reads = tuple(self._retriever.read(session, item, indexes) for item in selected)
        session = self._controller.record_reads(session, reads)
        await self._save(session)
        return session

    async def _select_v2_reads(
        self,
        payload: dict[str, Any],
        session: PlanningRetrievalSession,
        indexes: tuple[RevisionSemanticIndex, ...],
    ) -> PlanningRetrievalSession:
        planning_input = self._planning_input(payload)
        candidates = tuple(item for item in session.candidates if item.entry_kind == "semantic_unit")
        pages = session.read_pages
        if not pages:
            pages = CandidateDescriptorPager().pages(
                session,
                candidates,
                planning_input,
                context_window_tokens=getattr(self._model, "context_window_tokens", None),
                max_output_tokens=getattr(self._model, "max_output_tokens", session.frozen_resources.policy.output_token_reserve),
            )
            session = self._controller.record_read_pages(session, pages)
            await self._save(session)
        for page in pages[session.read_page_cursor:]:
            page_candidates = tuple(item for item in candidates if item.candidate_id in set(page.candidate_ids))
            proposal, session = await self._invoke(
                session,
                PlannerReadProposal,
                self._read_authority(),
                {
                    **planning_input,
                    "candidate_page": page.model_dump(mode="json"),
                    "candidates": tuple(CandidateDescriptorPager.descriptor(item) for item in page_candidates),
                },
                operation_id=f"read_page:{page.page_id}",
            )
            if session.state == "blocked":
                return session
            if len(set(proposal.candidate_ids)) != len(proposal.candidate_ids):
                return self._controller.block(session, "planner_candidate_duplicate", stage="read_selection", boundary="candidate_identity", operation_id=f"read_page:{page.page_id}")
            if not set(proposal.candidate_ids).issubset(page.candidate_ids):
                blocked = self._controller.block(session, "planner_candidate_unknown", stage="read_selection", boundary="candidate_identity", operation_id=f"read_page:{page.page_id}")
                await self._save(blocked)
                return blocked
            session = self._controller.record_page_selection(
                session,
                page_ordinal=page.ordinal,
                page_candidate_ids=page.candidate_ids,
                selected_ids=proposal.candidate_ids,
            )
            await self._save(session)
        if not session.selected_candidate_ids:
            return self._controller.block(session, "evidence_insufficient", stage="read_selection", boundary="selected_evidence")
        selected = tuple(item for item in session.candidates if item.candidate_id in set(session.selected_candidate_ids))
        if len(selected) > session.budget.max_exact_reads:
            return self._controller.block(session, "expansion_policy_limit", summary="精确读取超过冻结上限", stage="exact_read", boundary="max_exact_reads")
        reads = tuple(self._retriever.read(session, item, indexes) for item in selected)
        session = self._controller.record_reads(session, reads)
        await self._save(session)
        return session

    async def _plan_v2_work(self, payload: dict[str, Any], session: PlanningRetrievalSession):
        planning_input = self._planning_input(payload)
        pages = EvidenceReadPager().pages(
            session,
            session.reads,
            planning_input,
            context_window_tokens=getattr(self._model, "context_window_tokens", None),
            max_output_tokens=getattr(self._model, "max_output_tokens", session.frozen_resources.policy.output_token_reserve),
        )
        proposals: list[LaneAdviceProposal] = []
        for page in pages:
            allowed = {item.entry_id for item in page.reads}
            proposal, session = await self._invoke(
                session,
                LaneAdviceProposal,
                self._work_authority(),
                {
                    **planning_input,
                    "evidence_page": {"page_id": page.page_id, "ordinal": page.ordinal, "count": len(pages)},
                    "exact_reads": tuple(item.model_dump(mode="json") for item in page.reads),
                    "allowed_candidate_unit_ids": tuple(sorted(allowed)),
                },
                operation_id="work_spec" if len(pages) == 1 else f"work_page:{page.page_id}",
            )
            if session.state == "blocked":
                return None, session
            if any(
                unit_id not in allowed
                for draft in proposal.work_specs
                for requirement in draft.evidence_requirements
                for unit_id in requirement.candidate_unit_ids
            ):
                return None, self._controller.block(session, "planner_evidence_identity_unknown", stage="work_spec", boundary="exact_read_identity", operation_id=f"work_page:{page.page_id}")
            proposals.append(proposal)
        distinct: dict[str, WorkContextDraft] = {}
        for proposal in proposals:
            for draft in proposal.work_specs:
                distinct.setdefault(draft.model_dump_json(), draft)
        return LaneAdviceProposal(
            rationale=proposals[0].rationale if len(proposals) == 1 else "Evidence pages independently identified work contexts.",
            work_specs=combine_page_work_specs(tuple(distinct.values())),
        ), session

    async def _save(self, session: PlanningRetrievalSession) -> None:
        if self._checkpoint is not None:
            await self._checkpoint(session)

    async def _invoke(self, session, schema, authority: str, payload: dict[str, Any], *, operation_id: str):
        is_v2 = session.schema_version == "retrieval-session-v2"
        stage = self._operation_stage(operation_id)
        if is_v2 and operation_id in session.model_results:
            return schema.model_validate(session.model_results[operation_id]), session
        attempt_limit = session.frozen_resources.policy.max_model_attempts_per_operation if is_v2 else 1
        completed_attempts = sum(
            charge.operation_id.startswith(f"model:{operation_id}:attempt:")
            for charge in session.ledger.charges
        ) if is_v2 else 0
        for attempt in range(completed_attempts + 1, attempt_limit + 1):
            blocked = self._model_capacity_blocker(session, stage=stage, operation_id=operation_id)
            if blocked is not None:
                await self._save(blocked)
                return None, blocked
            error = None
            try:
                result = await self._model.invoke(schema, authority, payload)
            except Exception as exc:
                result = None
                error = exc
            usage = getattr(self._model, "last_usage", ModelUsage(model_calls=1))
            updated = self._controller.record_model_usage(
                session,
                model_calls=max(1, int(getattr(usage, "model_calls", 0))),
                tokens=max(0, int(getattr(usage, "input_tokens", 0))) + max(0, int(getattr(usage, "output_tokens", 0))),
                operation_id=f"{operation_id}:attempt:{attempt}" if is_v2 else operation_id,
                input_tokens=max(0, int(getattr(usage, "input_tokens", 0))),
                output_tokens=max(0, int(getattr(usage, "output_tokens", 0))),
                result_payload=result.model_dump(mode="json") if is_v2 and result is not None else None,
                result_operation_id=operation_id if is_v2 else None,
                usage_reported=getattr(self._model, "last_usage_reported", True),
                stage=stage,
            )
            blocked = self._model_capacity_blocker(updated, after_call=True, stage=stage, operation_id=operation_id)
            if blocked is not None:
                updated = blocked
            if error is not None and updated.state != "blocked" and (
                self._is_provider_window_failure(error) or attempt == attempt_limit
            ):
                code = "provider_request_window" if self._is_provider_window_failure(error) else "retrieval_planning_failed"
                summary = "模型单次请求超过配置窗口" if code == "provider_request_window" else "模型规划请求失败；实际调用用量已保留"
                updated = self._controller.block(updated, code, summary=summary, stage=stage, boundary="provider_request_window" if code == "provider_request_window" else "provider_failure", operation_id=operation_id)
            await self._save(updated)
            if updated.state == "blocked" or error is None:
                return result, updated
            session = updated
        blocked = self._controller.block(session, "retrieval_planning_failed", stage=stage, boundary="model_attempts", operation_id=operation_id)
        await self._save(blocked)
        return None, blocked

    def _model_capacity_blocker(
        self,
        session: PlanningRetrievalSession,
        *,
        after_call: bool = False,
        stage: str,
        operation_id: str,
    ):
        if session.frozen_resources is not None:
            frozen = session.frozen_resources
            comparison = (lambda used, limit: used > limit) if after_call else (lambda used, limit: used >= limit)
            for boundary, used, limit in (
                ("global_model_calls", session.usage.model_calls, frozen.global_model_calls_remaining),
                ("global_input_tokens", session.usage.input_tokens, frozen.global_input_tokens_remaining),
                ("global_output_tokens", session.usage.output_tokens, frozen.global_output_tokens_remaining),
            ):
                if comparison(used, limit):
                    return self._controller.block(session, "global_grant_exhausted", summary=f"全局授权 {boundary} 已耗尽", stage=stage, boundary=boundary, operation_id=operation_id)
        if not after_call and not self._controller.has_model_capacity(session):
            code = "expansion_policy_limit" if session.schema_version == "retrieval-session-v2" else "retrieval_budget_exhausted"
            boundary = "max_planner_model_calls" if session.usage.model_calls >= session.budget.max_model_calls else "max_planner_tokens"
            return self._controller.block(session, code, summary=f"冻结资源策略 {boundary} 已耗尽", stage=stage, boundary=boundary, operation_id=operation_id)
        return None

    @staticmethod
    def _operation_stage(operation_id: str) -> str:
        if operation_id == "query_plan":
            return "query_plan"
        if operation_id.startswith("read_page:") or operation_id == "read_selection":
            return "read_selection"
        return "work_spec"

    @staticmethod
    def _is_provider_window_failure(error: Exception) -> bool:
        code = str(getattr(error, "code", "") or "").casefold()
        return code in {"context_length_exceeded", "context_window_exceeded", "prompt_too_long"}

    async def _budget_blocked(self, session: PlanningRetrievalSession) -> RetrievalBackedPlanningResult:
        await self._save(session)
        return RetrievalBackedPlanningResult(
            session=session,
            blocker_code=session.blocker_code or "retrieval_budget_exhausted",
            blocker_summary=session.blocker_summary or ("planning retrieval model/token budget exhausted" if session.blocker_code == "retrieval_budget_exhausted" else "planning retrieval stage blocked"),
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
        derivation.pop("frozen_expansion_resources", None)
        catalog = derivation.get("portfolio_index_catalog")
        if isinstance(catalog, dict):
            derivation["portfolio_index_catalog"] = {
                "catalog_id": catalog.get("catalog_id"),
                "frontier_hash": catalog.get("frontier_hash"),
                "descriptors": catalog.get("descriptors") or (),
            }
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
        *,
        stage: str | None = None,
        boundary: str | None = None,
        operation_id: str | None = None,
    ) -> RetrievalBackedPlanningResult:
        terminal = self._controller.block(session, code, summary=summary, stage=stage, boundary=boundary, operation_id=operation_id)
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
