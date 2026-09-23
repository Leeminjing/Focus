r"""本文件对外提供 DossierDraft、EvidenceGroundedDossier、DossierValidator、ResolvedEvidenceDossierSynthesizer
与 WorkerResultDossierSynthesizer。

输入为冻结 observation、ResolvedEvidenceBundle 和受监督 worker 的结构化 dossier statements；输出为逐 statement 带精确
EvidenceRef 引用的 dossier，或显式 synthesis omission。具体工作流为严格解析 worker 输出、校验每个 citation 属于 bundle，
每个 statement 必须引用 bundle evidence，confirmed statement 还必须可在所引原文中逐字验证，否则降级为 hypothesis；
生产默认 synthesizer 按 requirement/question 组织已解析证据的精确内容；外部 worker 输出仍走相同 validator，未知引用或非法结构
使 synthesis 被省略且不改变原始证据。示例：
`result = await synthesizer.synthesize(observation, bundle)`。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    ResolvedEvidenceBundle,
    SemanticAuthority,
    WorkContextSpec,
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.context_curation import (
    EvidenceRef,
    NamespacedMessageRef,
    evidence_ref_key,
)


class _DossierModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DossierStatement(_DossierModel):
    requirement_id: str = Field(default="unscoped", min_length=1, max_length=120)
    question: str | None = Field(default=None, max_length=2000)
    statement: str = Field(min_length=1, max_length=4000)
    authority: SemanticAuthority = "confirmed"
    citations: tuple[EvidenceRef, ...] = ()

    @model_validator(mode="after")
    def require_confirmation_support(self) -> DossierStatement:
        keys = [evidence_ref_key(ref) for ref in self.citations]
        if len(keys) != len(set(keys)):
            raise ValueError("dossier statement citation 重复")
        if not keys:
            raise ValueError("dossier statement 必须引用 evidence")
        return self


class DossierDraft(_DossierModel):
    statements: tuple[DossierStatement, ...]


class EvidenceGroundedDossier(_DossierModel):
    dossier_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    statements: tuple[DossierStatement, ...]


class DossierBuildResult(_DossierModel):
    dossier: EvidenceGroundedDossier | None = None
    omitted_reason: str | None = None

    @model_validator(mode="after")
    def require_one_result(self) -> DossierBuildResult:
        if (self.dossier is None) == (self.omitted_reason is None):
            raise ValueError("dossier build result 必须恰好包含 dossier 或 omission")
        return self


class DossierSynthesizerPort(Protocol):
    async def synthesize(
        self,
        observation: LoopObservationEnvelope,
        bundle: ResolvedEvidenceBundle,
    ) -> DossierBuildResult: ...


class DossierValidator:
    def validate(
        self,
        bundle: ResolvedEvidenceBundle,
        draft: DossierDraft,
    ) -> EvidenceGroundedDossier:
        allowed = {evidence_ref_key(ref) for ref in bundle.evidence_frontier}
        validated = []
        for statement in draft.statements:
            unknown = {evidence_ref_key(ref) for ref in statement.citations} - allowed
            if unknown:
                raise ValueError("dossier statement 引用了 resolved bundle 之外的 evidence")
            authority = statement.authority
            if authority == "confirmed" and not any(
                statement.statement.casefold() in self.content(bundle, ref).casefold()
                for ref in statement.citations
            ):
                authority = "hypothesis"
            validated.append(statement.model_copy(update={"authority": authority}))
        statements = tuple(validated)
        identity = stable_expansion_hash(
            "evidence-grounded-dossier",
            bundle.resolution_id,
            tuple(statement.model_dump(mode="json") for statement in statements),
        )
        return EvidenceGroundedDossier(
            dossier_id=identity,
            resolution_id=bundle.resolution_id,
            statements=statements,
        )

    @staticmethod
    def content(bundle: ResolvedEvidenceBundle, ref: EvidenceRef) -> str:
        if isinstance(ref, NamespacedMessageRef):
            content = bundle.evidence.message(ref).content
        else:
            content = bundle.evidence.structured_item(ref).content
        if isinstance(content, str):
            return content
        return str(content)


class WorkerResultDossierSynthesizer:
    def __init__(self, validator: DossierValidator | None = None) -> None:
        self._validator = validator or DossierValidator()

    async def synthesize(
        self,
        observation: LoopObservationEnvelope,
        bundle: ResolvedEvidenceBundle,
    ) -> DossierBuildResult:
        results = tuple(
            item
            for item in observation.worker_results
            if item.get("kind") == "dossier_synthesizer"
        )
        if not results:
            return DossierBuildResult(omitted_reason="dossier worker result unavailable")
        successful = tuple(
            item.get("result") or {}
            for item in results
            if item.get("status") in {"success", "proposed", "consumed"}
        )
        if not successful:
            return DossierBuildResult(omitted_reason="dossier worker failed")
        try:
            draft = TypeAdapter(DossierDraft).validate_python(successful[-1])
            dossier = self._validator.validate(bundle, draft)
        except (ValidationError, TypeError, ValueError) as exc:
            return DossierBuildResult(omitted_reason=f"dossier validation failed: {str(exc)[:1600]}")
        return DossierBuildResult(dossier=dossier)


class ResolvedEvidenceDossierSynthesizer:
    def __init__(self, validator: DossierValidator | None = None) -> None:
        self._validator = validator or DossierValidator()

    async def synthesize(
        self,
        observation: LoopObservationEnvelope,
        bundle: ResolvedEvidenceBundle,
    ) -> DossierBuildResult:
        try:
            requirements = {
                item.requirement_id: item
                for item in self._work_spec(observation, bundle).evidence_requirements
            }
            statements = tuple(
                DossierStatement(
                    requirement_id=item.requirement_id,
                    question=requirements[item.requirement_id].question,
                    statement=self._validator.content(bundle, item.ref)[:4000],
                    citations=(item.ref,),
                )
                for item in bundle.items
            )
            return DossierBuildResult(
                dossier=self._validator.validate(bundle, DossierDraft(statements=statements))
            )
        except (KeyError, TypeError, ValueError) as exc:
            return DossierBuildResult(omitted_reason=f"dossier construction failed: {str(exc)[:1600]}")

    @staticmethod
    def _work_spec(
        observation: LoopObservationEnvelope,
        bundle: ResolvedEvidenceBundle,
    ) -> WorkContextSpec:
        assessment = observation.expansion_assessment or {}
        for opportunity in assessment.get("opportunities") or ():
            work_spec = opportunity.get("work_spec") or {}
            if work_spec.get("work_spec_id") == bundle.work_spec_id:
                return WorkContextSpec.model_validate(work_spec)
        raise ValueError("dossier 无法定位 resolved bundle 对应的冻结 WorkContextSpec")


def render_dossier(dossier: EvidenceGroundedDossier) -> str:
    lines = ["Evidence-grounded Dossier"]
    for index, statement in enumerate(dossier.statements, start=1):
        citations = ", ".join("/".join(evidence_ref_key(ref)) for ref in statement.citations)
        question = f" {statement.question}" if statement.question else ""
        lines.append(
            f"{index}. [{statement.requirement_id}/{statement.authority}]{question}\n"
            f"   {statement.statement} [evidence: {citations}]"
        )
    return "\n".join(lines)
