r"""本文件对外提供 CognitivePlannerPort 与 WorkerResultCognitivePlanner。

输入为冻结 observation、结构化 signal 集合、retrieval-session 验证的 manifests 以及受监督 Curator worker 的结构化结果；
输出为 `CognitivePlanResult` 中零个、一个或多个稳定 `WorkContextSpec`，或显式 planning failure。具体工作流为先传播显式 retrieval
blocker，再严格解析 `WorkContextDraft`、校验新版 session 的 Context unit 与类型化 Mission candidate 已在同一冻结来源内精读并冻结 planner version；
本层不选择最终证据、不做同义归并或提交 Context。
示例：`result = await WorkerResultCognitivePlanner().plan(observation, signals, manifests)`。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    CognitivePlanResult,
    ContextSemanticManifest,
    DerivationStageFailure,
    WorkContextDraft,
    WorkContextSpec,
)
from backend.app.desktop.agent_loop.context_expansion.signals import ExpansionSignalSet
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import ExactEvidenceRead, PlanningRetrievalSession
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope

_DRAFTS = TypeAdapter(tuple[WorkContextDraft, ...])


class PlannerEvidenceIdentityError(ValueError):
    pass


class CognitivePlannerPort(Protocol):
    VERSION: str

    async def plan(
        self,
        observation: LoopObservationEnvelope,
        signals: ExpansionSignalSet,
        manifests: tuple[ContextSemanticManifest, ...],
    ) -> CognitivePlanResult: ...


class WorkerResultCognitivePlanner:
    VERSION = "retrieval-worker-cognitive-planner-v2"

    async def plan(
        self,
        observation: LoopObservationEnvelope,
        signals: ExpansionSignalSet,
        manifests: tuple[ContextSemanticManifest, ...],
    ) -> CognitivePlanResult:
        results = tuple(
            item
            for item in observation.worker_results
            if item.get("kind") == "lane_curator"
        )
        if not results:
            if not observation.portfolio_frontier:
                return CognitivePlanResult()
            return self._failure("planner_result_missing", "冻结 observation 没有可消费的 cognitive planner 结果", True)
        failures = tuple(item for item in results if item.get("status") in {"error", "failed"})
        payloads = tuple(
            item.get("result") or {}
            for item in results
            if item.get("status") in {"success", "proposed", "consumed"}
        )
        if not payloads and failures:
            return self._failure("planner_worker_failed", "全部 cognitive planner worker 均失败", True)
        blockers = tuple(payload.get("planning_blocker") for payload in payloads if payload.get("planning_blocker"))
        if blockers:
            first = dict(blockers[0] or {})
            return self._failure(
                str(first.get("code") or "retrieval_planning_failed"),
                str(first.get("summary") or "retrieval-backed planning blocked")[:1800],
                False,
            )
        try:
            drafts = tuple(
                draft
                for payload in payloads
                for draft in self._drafts_from_payload(payload, observation)
            )
        except PlannerEvidenceIdentityError:
            return self._failure("planner_evidence_identity_unknown", "planner 引用了同一冻结 session 未精读的 semantic unit", False)
        except ValidationError as exc:
            return self._failure("planner_contract_invalid", str(exc)[:1800], False)
        except ValueError:
            return self._failure("planner_contract_invalid", "新版 planning result 与冻结 session provenance 不一致", False)
        known_units = {unit.unit_id for manifest in manifests for unit in manifest.units}
        invented = sorted(
            {
                unit_id
                for draft in drafts
                for requirement in draft.evidence_requirements
                for unit_id in requirement.candidate_unit_ids
                if unit_id not in known_units
            }
        )
        if invented:
            return self._failure(
                "planner_evidence_identity_unknown",
                "planner 引用了冻结 manifests 中不存在的 semantic unit: " + ", ".join(invented[:8]),
                False,
            )
        specs: list[WorkContextSpec] = []
        required: set[str] = set()
        for draft in drafts:
            spec = draft.freeze(self.VERSION)
            specs.append(spec)
            if draft.required:
                required.add(spec.work_spec_id)
        work_specs = tuple(sorted(specs, key=lambda item: item.work_spec_id))
        return CognitivePlanResult(
            work_specs=work_specs,
            required_work_spec_ids=tuple(sorted(required)),
        )

    @staticmethod
    def _drafts_from_payload(payload: dict, observation: LoopObservationEnvelope) -> tuple[WorkContextDraft, ...]:
        drafts = _DRAFTS.validate_python(payload.get("work_specs") or ())
        raw_session = payload.get("planning_session") or {}
        if raw_session.get("schema_version") != "retrieval-session-v2":
            return drafts
        session = PlanningRetrievalSession.model_validate(raw_session)
        if session.state != "planned" or session.frontier_hash != observation.observed_frontier_hash:
            raise ValueError("planning result frontier/state 不一致")
        if session.frozen_resources.grant_revision != observation.authority_revision:
            raise ValueError("planning result grant revision 已过期")
        visible_contexts = {item.get("context_id") for item in observation.portfolio_frontier}
        if not set(session.source_context_ids).issubset(visible_contexts):
            raise ValueError("planning result source Context 越界")
        reads = tuple(ExactEvidenceRead.model_validate(item) for item in payload.get("exact_reads") or ())
        if reads != session.reads:
            raise ValueError("planning result exact reads 与冻结 session 不一致")
        allowed = {item.entry_id for item in reads if item.source_type == "context"}
        read_ids = {item.candidate_id for item in reads}
        read_candidates = {
            item.candidate_id: item.evidence_identity()
            for item in session.candidates
            if item.candidate_id in read_ids
        }
        if any(
            unit_id not in allowed
            for draft in drafts
            for requirement in draft.evidence_requirements
            for unit_id in requirement.candidate_unit_ids
        ):
            raise PlannerEvidenceIdentityError("WorkSpec candidate unit 未在同一 session 精读")
        if any(
            read_candidates.get(ref.candidate_id) != ref
            or (ref.mission_ref is not None and (
                ref.mission_ref.loop_id != observation.loop_id
                or ref.mission_ref.goal_revision != observation.goal_revision
            ))
            for draft in drafts
            for requirement in draft.evidence_requirements
            for ref in requirement.candidate_refs
        ):
            raise PlannerEvidenceIdentityError("WorkSpec candidate 来源未在当前 Mission/session 精读")
        return drafts

    @staticmethod
    def _failure(code: str, summary: str, retryable: bool) -> CognitivePlanResult:
        return CognitivePlanResult(
            failure=DerivationStageFailure(
                stage="cognitive_planning",
                code=code,
                summary=summary,
                retryable=retryable,
            )
        )
