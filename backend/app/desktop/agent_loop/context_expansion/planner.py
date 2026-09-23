r"""本文件对外提供 CognitivePlannerPort 与 WorkerResultCognitivePlanner。

输入为冻结 observation、结构化 signal 集合、evidence-grounded manifests 以及受监督 Curator worker 的结构化结果；
输出为 `CognitivePlanResult` 中零个、一个或多个稳定 `WorkContextSpec`，或显式 planning failure。具体工作流为
严格解析 `WorkContextDraft`、校验 candidate semantic-unit identity、冻结 planner version 并去重，不选择最终证据或提交 Context。
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
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope

_DRAFTS = TypeAdapter(tuple[WorkContextDraft, ...])


class CognitivePlannerPort(Protocol):
    VERSION: str

    async def plan(
        self,
        observation: LoopObservationEnvelope,
        signals: ExpansionSignalSet,
        manifests: tuple[ContextSemanticManifest, ...],
    ) -> CognitivePlanResult: ...


class WorkerResultCognitivePlanner:
    VERSION = "worker-cognitive-planner-v1"

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
        try:
            drafts = tuple(
                draft
                for payload in payloads
                for draft in _DRAFTS.validate_python(payload.get("work_specs") or ())
            )
        except ValidationError as exc:
            return self._failure("planner_contract_invalid", str(exc)[:1800], False)
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
        by_identity: dict[str, WorkContextSpec] = {}
        required: set[str] = set()
        for draft in drafts:
            spec = draft.freeze(self.VERSION)
            by_identity.setdefault(spec.work_spec_id, spec)
            if draft.required:
                required.add(spec.work_spec_id)
        work_specs = tuple(sorted(by_identity.values(), key=lambda item: item.work_spec_id))
        return CognitivePlanResult(
            work_specs=work_specs,
            required_work_spec_ids=tuple(sorted(required)),
        )

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
