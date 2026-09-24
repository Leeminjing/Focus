r"""本文件对外提供 ContextQualityPreflight、ContextQualityAssessment、QualityDimensionVerdict 与 ContextQualityVerifier。

输入为同一冻结 WorkContextSpec、ResolvedEvidenceBundle、ValidatedContextDossier 及受监督三维 verdict；输出为绑定全部输入 identity
的可审计质量评估或 fail-closed blocker。具体工作流为先机械验证 requirement/question coverage、citation membership、每项 evidence
的 requirement/claim/protocol 用途与 workspace 边界，再只接受 minimality、sufficiency、coherence 三个独立语义 verdict；三者必须
同时 pass 才允许 compilation。示例：`assessment = verifier.verify(work_spec, bundle, dossier, worker_payload)`。
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    ResolvedEvidenceBundle,
    WorkContextSpec,
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.context_expansion.synthesis import (
    ContextSynthesisValidator,
    ValidatedContextDossier,
)
from backend.app.desktop.context_curation import evidence_ref_key

QualityDimension = Literal["minimality", "sufficiency", "coherence"]
QualityVerdict = Literal["pass", "fail", "unknown"]


class _QualityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextQualityPreflight(_QualityModel):
    preflight_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_spec_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    dossier_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible: bool
    blocker_codes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    unused_evidence_keys: tuple[tuple[str, ...], ...] = ()


class QualityDimensionVerdict(_QualityModel):
    dimension: QualityDimension
    verdict: QualityVerdict
    reasons: tuple[str, ...] = Field(min_length=1)
    evidence_keys: tuple[tuple[str, ...], ...] = ()
    requirement_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    @field_validator("reasons", "evidence_keys", "requirement_ids", "claim_ids", "conflicts", mode="before")
    @classmethod
    def normalize_values(cls, values):
        return tuple(sorted({tuple(item) if isinstance(item, list) else item for item in (values or ())}))


class ContextQualityAssessment(_QualityModel):
    assessment_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_spec_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    dossier_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    preflight_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_version: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    dimensions: tuple[QualityDimensionVerdict, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def require_complete_stable_assessment(self) -> Self:
        by_dimension = {item.dimension: item for item in self.dimensions}
        if set(by_dimension) != {"minimality", "sufficiency", "coherence"}:
            raise ValueError("quality assessment 必须恰好包含三个 required dimensions")
        expected = stable_expansion_hash(
            "context-quality-assessment",
            self.work_spec_id,
            self.resolution_id,
            self.dossier_id,
            self.preflight_id,
            self.verifier_version,
            self.policy_version,
            tuple(item.model_dump(mode="json") for item in self.dimensions),
        )
        if self.assessment_id != expected:
            raise ValueError("quality assessment identity 与冻结输入不一致")
        return self

    @property
    def passes(self) -> bool:
        return all(item.verdict == "pass" for item in self.dimensions)

    @classmethod
    def create(
        cls,
        *,
        work_spec: WorkContextSpec,
        bundle: ResolvedEvidenceBundle,
        dossier: ValidatedContextDossier,
        preflight: ContextQualityPreflight,
        verifier_version: str,
        policy_version: str,
        dimensions: tuple[QualityDimensionVerdict, ...],
    ) -> Self:
        if (
            preflight.work_spec_id != work_spec.work_spec_id
            or preflight.resolution_id != bundle.resolution_id
            or preflight.dossier_id != dossier.dossier_id
        ):
            raise ValueError("quality preflight identities 与冻结 inputs 不一致")
        known_requirements = {item.requirement_id for item in work_spec.evidence_requirements}
        known_claims = {item.claim_id for item in dossier.claims}
        known_evidence = {evidence_ref_key(item) for item in bundle.evidence_frontier}
        for dimension in dimensions:
            if set(dimension.requirement_ids) - known_requirements:
                raise ValueError("quality verdict 引用了未知 requirement identity")
            if set(dimension.claim_ids) - known_claims:
                raise ValueError("quality verdict 引用了未知 claim identity")
            if set(dimension.evidence_keys) - known_evidence:
                raise ValueError("quality verdict 引用了 bundle 之外的 evidence identity")
        ordered = tuple(sorted(dimensions, key=lambda item: item.dimension))
        payload = (
            work_spec.work_spec_id,
            bundle.resolution_id,
            dossier.dossier_id,
            preflight.preflight_id,
            verifier_version,
            policy_version,
            tuple(item.model_dump(mode="json") for item in ordered),
        )
        return cls(
            assessment_id=stable_expansion_hash("context-quality-assessment", *payload),
            work_spec_id=work_spec.work_spec_id,
            resolution_id=bundle.resolution_id,
            dossier_id=dossier.dossier_id,
            preflight_id=preflight.preflight_id,
            verifier_version=verifier_version,
            policy_version=policy_version,
            dimensions=ordered,
        )


class ContextQualityResult(_QualityModel):
    preflight: ContextQualityPreflight
    assessment: ContextQualityAssessment | None = None
    blocker_code: str | None = None
    blocker_summary: str | None = None
    attempt_records: tuple[dict[str, Any], ...] = ()

    @model_validator(mode="after")
    def require_result_shape(self) -> Self:
        blocked = self.blocker_code is not None or self.blocker_summary is not None
        if blocked and not (self.blocker_code and self.blocker_summary):
            raise ValueError("quality blocker 必须包含 code 与 summary")
        if self.assessment is None and not blocked:
            raise ValueError("quality result 缺少 assessment 或 blocker")
        if self.assessment is not None and self.assessment.passes and blocked:
            raise ValueError("passing quality assessment 不得携带 blocker")
        if self.assessment is not None and not self.assessment.passes and not blocked:
            raise ValueError("non-passing quality assessment 必须 fail closed")
        return self


class ContextQualityVerifier:
    VERSION = "context-quality-verifier-v1"
    POLICY_VERSION = "derived-context-quality-policy-v1"

    def preflight(
        self,
        work_spec: WorkContextSpec,
        bundle: ResolvedEvidenceBundle,
        dossier: ValidatedContextDossier,
    ) -> ContextQualityPreflight:
        codes: list[str] = []
        reasons: list[str] = []
        if bundle.work_spec_id != work_spec.work_spec_id or dossier.work_spec_id != work_spec.work_spec_id:
            codes.append("work_spec_identity_mismatch")
            reasons.append("WorkSpec identity 与 resolution/dossier 不一致")
        if dossier.resolution_id != bundle.resolution_id:
            codes.append("resolution_identity_mismatch")
            reasons.append("Dossier 未绑定当前 ResolvedEvidenceBundle")
        allowed = {evidence_ref_key(item) for item in bundle.evidence_frontier}
        cited = {evidence_ref_key(ref) for claim in dossier.claims for ref in claim.citations}
        if cited - allowed:
            codes.append("citation_outside_bundle")
            reasons.append("Dossier citation 超出 resolved evidence frontier")
        required = {
            item.requirement_id
            for item in work_spec.evidence_requirements
            if item.necessity == "required"
        }
        covered_requirements = {identity for claim in dossier.claims for identity in claim.requirement_ids}
        if not required.issubset(covered_requirements):
            codes.append("required_requirement_uncovered")
            reasons.append("Dossier claim graph 未覆盖全部 required requirements")
        question_ids = {ContextSynthesisValidator.question_id(item) for item in work_spec.questions}
        covered_questions = {identity for claim in dossier.claims for identity in claim.question_ids}
        unresolved = {ContextSynthesisValidator.question_id(item) for item in dossier.unresolved_questions}
        if not question_ids.issubset(covered_questions | unresolved):
            codes.append("required_question_uncovered")
            reasons.append("Dossier 未回答或显式保留全部 WorkSpec questions")
        requirement_evidence = {evidence_ref_key(item.ref) for item in bundle.items}
        protocol = self._protocol_closure(bundle, requirement_evidence | cited)
        used = requirement_evidence | cited | protocol
        unused = tuple(sorted(allowed - used))
        if unused:
            codes.append("evidence_without_purpose")
            reasons.append("Resolved bundle 包含未服务 requirement、claim 或 Tool Exchange closure 的 evidence")
        if work_spec.workspace_requirement == "read_only" and any(
            item.authority == "confirmed" and not item.citations
            for item in dossier.claims
        ):
            codes.append("workspace_boundary_inconsistent")
            reasons.append("read-only dossier 包含无来源的执行性事实")
        inputs = (
            work_spec.work_spec_id,
            bundle.resolution_id,
            dossier.dossier_id,
            tuple(sorted(codes)),
            tuple(sorted(reasons)),
            unused,
            self.POLICY_VERSION,
        )
        return ContextQualityPreflight(
            preflight_id=stable_expansion_hash("context-quality-preflight", *inputs),
            work_spec_id=work_spec.work_spec_id,
            resolution_id=bundle.resolution_id,
            dossier_id=dossier.dossier_id,
            eligible=not codes,
            blocker_codes=tuple(sorted(codes)),
            reasons=tuple(sorted(reasons)),
            unused_evidence_keys=unused,
        )

    def verify(
        self,
        work_spec: WorkContextSpec,
        bundle: ResolvedEvidenceBundle,
        dossier: ValidatedContextDossier,
        worker_payload: dict[str, Any] | None,
    ) -> ContextQualityResult:
        preflight = self.preflight(work_spec, bundle, dossier)
        if not preflight.eligible:
            return ContextQualityResult(
                preflight=preflight,
                blocker_code="quality_preflight_failed",
                blocker_summary="; ".join(preflight.reasons)[:2000],
            )
        if worker_payload is None:
            return ContextQualityResult(
                preflight=preflight,
                blocker_code="quality_worker_missing",
                blocker_summary="缺少 context_quality_verifier 的独立三维评估",
            )
        try:
            dimensions = TypeAdapter(tuple[QualityDimensionVerdict, ...]).validate_python(
                worker_payload.get("dimensions") or ()
            )
            assessment = ContextQualityAssessment.create(
                work_spec=work_spec,
                bundle=bundle,
                dossier=dossier,
                preflight=preflight,
                verifier_version=self.VERSION,
                policy_version=self.POLICY_VERSION,
                dimensions=dimensions,
            )
        except (TypeError, ValidationError, ValueError) as exc:
            return ContextQualityResult(
                preflight=preflight,
                blocker_code="quality_contract_invalid",
                blocker_summary=f"quality verifier output invalid: {str(exc)[:1600]}",
            )
        if not assessment.passes:
            failed = ", ".join(
                f"{item.dimension}={item.verdict}"
                for item in assessment.dimensions
                if item.verdict != "pass"
            )
            return ContextQualityResult(
                preflight=preflight,
                assessment=assessment,
                blocker_code="context_quality_failed",
                blocker_summary=f"derived Context quality 未通过: {failed}",
            )
        return ContextQualityResult(preflight=preflight, assessment=assessment)

    @staticmethod
    def _protocol_closure(
        bundle: ResolvedEvidenceBundle,
        selected_keys: set[tuple[str, ...]],
    ) -> set[tuple[str, ...]]:
        required: set[tuple[str, ...]] = set()
        for source in bundle.evidence.sources:
            calls = {
                str(call.get("id") or ""): message.ref
                for message in source.messages
                for call in message.tool_calls
                if call.get("id")
            }
            results = {
                str(message.tool_call_id): message.ref
                for message in source.messages
                if message.tool_call_id
            }
            for call_id, call_ref in calls.items():
                result_ref = results.get(call_id)
                if result_ref is None:
                    continue
                pair = {evidence_ref_key(call_ref), evidence_ref_key(result_ref)}
                if pair & selected_keys:
                    required.update(pair)
        return required
