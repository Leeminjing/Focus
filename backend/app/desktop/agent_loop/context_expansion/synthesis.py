r"""本文件对外提供 Context synthesis DTO、ContextSynthesisValidator、WorkerResultContextSynthesizer 与渲染函数。

输入为冻结 WorkContextSpec、ResolvedEvidenceBundle、原子 claims、引用/前提/覆盖映射及独立支持判定；输出为身份稳定的
ValidatedContextDossier 或明确 blocker。具体工作流为作者 citation_catalog/schema_for 仅允许冻结目录键，解析绑定回原类型化引用，
Schema 表达 confirmed/inference/hypothesis 的既有引用与前提规则；空目录拒绝，通用 DTO 保留完整引用 JSON 恢复。
section 内嵌清单是唯一声明归属；materialize 按拓扑生成稳定身份，拒绝重复 key、未知 premise、循环和非法 authority。
statement 表达来源领域命题，生成图的分组/依赖/覆盖由类型化字段表达。validate_draft 在成功记录前检查冻结引用和完整覆盖，
validate 再独立检查 direct-support。ClaimSupportProposal.schema_for 约束 confirmed 身份与数量，不预设 verdict。
失败摘要仅保留实际拒绝计数及首条身份；ContextSynthesisReview 在私有失败产物保存已结构准入候选与实际判定，未核验为 None，
不进入 ready 缓存。持久引用先类型化再排序，冻结输入及 synthesizer version 决定 dossier 身份，不读取最新 Portfolio 或补造证据。
示例：`dossier = validator.validate(work_spec, bundle, draft, support_verdicts)`。
"""

from __future__ import annotations

import json
from enum import Enum
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Any, Literal, Protocol, Self, Union

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    ResolvedEvidenceBundle,
    WorkContextSpec,
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.context_curation import EvidenceRef, evidence_ref_key

ClaimAuthority = Literal["confirmed", "inference", "hypothesis"]
ClaimSupportVerdict = Literal["supported", "unsupported", "unknown"]


class _SynthesisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextSynthesisClaim(_SynthesisModel):
    claim_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    statement: str = Field(min_length=1, max_length=2000)
    authority: ClaimAuthority
    citations: tuple[EvidenceRef, ...] = ()
    premise_claim_ids: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    question_ids: tuple[str, ...] = ()

    @field_validator("premise_claim_ids", "requirement_ids", "question_ids", mode="before")
    @classmethod
    def normalize_identities(cls, values: Any) -> tuple[str, ...]:
        return tuple(sorted(set(values or ())))

    @field_validator("citations", mode="after")
    @classmethod
    def order_citations(cls, values: tuple[EvidenceRef, ...]) -> tuple[EvidenceRef, ...]:
        return tuple(sorted(values, key=evidence_ref_key))

    @model_validator(mode="after")
    def require_authority_shape(self) -> Self:
        keys = tuple(evidence_ref_key(item) for item in self.citations)
        if len(keys) != len(set(keys)):
            raise ValueError("synthesis claim citation identity 重复")
        if "\n" in self.statement:
            raise ValueError("synthesis claim 必须是单个原子陈述")
        if self.authority == "confirmed" and (not self.citations or self.premise_claim_ids):
            raise ValueError("confirmed claim 必须直接引用 evidence 且不得依赖推理 premise")
        if self.authority == "inference" and not self.premise_claim_ids:
            raise ValueError("inference claim 必须声明 premise claims")
        if self.authority == "hypothesis" and not (self.citations or self.premise_claim_ids):
            raise ValueError("hypothesis claim 必须保留 evidence 或 premise lineage")
        expected = stable_expansion_hash(
            "context-synthesis-claim",
            self.statement,
            self.authority,
            keys,
            self.premise_claim_ids,
            self.requirement_ids,
            self.question_ids,
        )
        if self.claim_id != expected:
            raise ValueError("synthesis claim identity 与内容不一致")
        return self

    @classmethod
    def create(
        cls,
        *,
        statement: str,
        authority: ClaimAuthority,
        citations: tuple[EvidenceRef, ...] = (),
        premise_claim_ids: tuple[str, ...] = (),
        requirement_ids: tuple[str, ...] = (),
        question_ids: tuple[str, ...] = (),
    ) -> Self:
        ordered_citations = tuple(sorted(citations, key=evidence_ref_key))
        premises = tuple(sorted(set(premise_claim_ids)))
        requirements = tuple(sorted(set(requirement_ids)))
        questions = tuple(sorted(set(question_ids)))
        identity = stable_expansion_hash(
            "context-synthesis-claim",
            statement,
            authority,
            tuple(evidence_ref_key(item) for item in ordered_citations),
            premises,
            requirements,
            questions,
        )
        return cls(
            claim_id=identity,
            statement=statement,
            authority=authority,
            citations=ordered_citations,
            premise_claim_ids=premises,
            requirement_ids=requirements,
            question_ids=questions,
        )


class SynthesisClaimDraft(_SynthesisModel):
    claim_key: str = Field(min_length=1, max_length=120)
    statement: str = Field(min_length=1, max_length=2000,
        description="关于冻结来源领域的单个命题。当前输出的分组、引用、推理依赖和覆盖关系只由对应类型化字段表达，不作为该命题的一部分。")
    authority: ClaimAuthority
    citations: tuple[EvidenceRef, ...] = ()
    premise_claim_keys: tuple[str, ...] = Field(default=(),
        description="本次输出中已声明的本地 claim_key 依赖；这是生成图的结构关系，不需要另写一条 confirmed 声明描述该关系。")
    requirement_ids: tuple[str, ...] = ()
    question_ids: tuple[str, ...] = ()


class SynthesisSectionDraft(_SynthesisModel):
    title: str = Field(min_length=1, max_length=200)
    claims: tuple[SynthesisClaimDraft, ...] = Field(min_length=1)


class ContextSynthesisWorkerDraft(_SynthesisModel):
    sections: tuple[SynthesisSectionDraft, ...] = Field(min_length=1)
    unresolved_questions: tuple[str, ...] = ()

    @staticmethod
    def citation_catalog(bundle: ResolvedEvidenceBundle) -> dict[str, EvidenceRef]:
        return {
            stable_expansion_hash("synthesis-citation-input-v1", evidence_ref_key(ref)): ref
            for ref in sorted(bundle.evidence_frontier, key=evidence_ref_key)
        }

    @classmethod
    def schema_for(cls, bundle: ResolvedEvidenceBundle) -> type[ContextSynthesisWorkerDraft]:
        catalog = cls.citation_catalog(bundle)
        if not catalog:
            raise ValueError("synthesis 作者缺少冻结 citation，无法构造合法声明根")
        identity = stable_expansion_hash("synthesis-author-contract-v1", bundle.resolution_id, tuple(catalog))[:12]
        choices = Enum(f"FrozenCitationChoices_{identity}",
            {f"citation_{index}": key for index, key in enumerate(catalog)}, type=str)

        def resolve(value: Any) -> EvidenceRef:
            if not isinstance(value, str) or value not in catalog:
                raise ValueError("citation 必须选择本次冻结目录中的身份")
            return catalog[value]

        citation = Annotated[EvidenceRef, BeforeValidator(resolve, json_schema_input_type=choices)]
        variants = cls._claim_shapes(citation, identity)
        section = create_model(f"FrozenSynthesisSection_{identity}", __base__=SynthesisSectionDraft,
            claims=(tuple[Union[variants], ...], Field(min_length=1)))
        return create_model(f"FrozenSynthesisWorker_{identity}", __base__=cls,
            sections=(tuple[section, ...], Field(min_length=1)))

    @staticmethod
    def _claim_shapes(citation: Any, identity: str) -> tuple[type[SynthesisClaimDraft], ...]:
        shapes = (
            ("Confirmed", "confirmed", Field(min_length=1), Field(default=(), max_length=0)),
            ("Inference", "inference", Field(default=()), Field(min_length=1)),
            ("EvidenceHypothesis", "hypothesis", Field(min_length=1), Field(default=())),
            ("PremiseHypothesis", "hypothesis", Field(default=(), max_length=0), Field(min_length=1)),
        )
        return tuple(create_model(f"Frozen{name}_{identity}", __base__=SynthesisClaimDraft,
            authority=(Literal[authority], ...), citations=(tuple[citation, ...], citations),
            premise_claim_keys=(tuple[str, ...], premises))
            for name, authority, citations, premises in shapes)

    def materialize(self) -> ContextSynthesisDraft:
        by_key: dict[str, ContextSynthesisClaim] = {}
        for draft in self._ordered_claims(self.sections):
            by_key[draft.claim_key] = ContextSynthesisClaim.create(
                statement=draft.statement,
                authority=draft.authority,
                citations=draft.citations,
                premise_claim_ids=tuple(by_key[key].claim_id for key in draft.premise_claim_keys),
                requirement_ids=draft.requirement_ids,
                question_ids=draft.question_ids,
            )
        sections = tuple(SynthesisSection(title=section.title,
            claim_ids=tuple(by_key[draft.claim_key].claim_id for draft in section.claims)) for section in self.sections)
        return ContextSynthesisDraft(
            sections=sections,
            claims=tuple(by_key.values()),
            unresolved_questions=self.unresolved_questions,
        )

    @staticmethod
    def _ordered_claims(sections: tuple[SynthesisSectionDraft, ...]) -> tuple[SynthesisClaimDraft, ...]:
        drafts: dict[str, SynthesisClaimDraft] = {}
        for section in sections:
            for draft in section.claims:
                if draft.claim_key in drafts:
                    raise ValueError("synthesis worker claim_key 重复")
                drafts[draft.claim_key] = draft
        if {key for draft in drafts.values() for key in draft.premise_claim_keys} - drafts.keys():
            raise ValueError("synthesis worker premise 必须引用已知的 claim_key")
        try:
            order = tuple(TopologicalSorter({key: draft.premise_claim_keys for key, draft in drafts.items()}).static_order())
        except CycleError as exc:
            raise ValueError("synthesis worker premise graph 必须无环") from exc
        return tuple(drafts[key] for key in order)


class SynthesisSection(_SynthesisModel):
    title: str = Field(min_length=1, max_length=200)
    claim_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("claim_ids", mode="before")
    @classmethod
    def normalize_claims(cls, values: Any) -> tuple[str, ...]:
        return tuple(dict.fromkeys(values or ()))


class ContextSynthesisDraft(_SynthesisModel):
    sections: tuple[SynthesisSection, ...] = Field(min_length=1)
    claims: tuple[ContextSynthesisClaim, ...] = Field(min_length=1)
    unresolved_questions: tuple[str, ...] = ()


class ClaimSupportAssessment(_SynthesisModel):
    claim_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: ClaimSupportVerdict
    citation_keys: tuple[tuple[str, ...], ...]
    reason: str = Field(min_length=1, max_length=2000)


class ClaimSupportDraft(_SynthesisModel):
    claim_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: ClaimSupportVerdict
    reason: str = Field(min_length=1, max_length=2000)


class ClaimSupportProposal(_SynthesisModel):
    assessments: tuple[ClaimSupportDraft, ...]

    @staticmethod
    def schema_for(claim_ids: tuple[str, ...]) -> type[ClaimSupportProposal]:
        identities = tuple(sorted(claim_ids))
        if len(identities) != len(set(identities)):
            raise ValueError("confirmed claim identity 重复")
        identity = stable_expansion_hash("claim-support-output-contract-v1", identities)[:12]
        assessment = create_model(
            f"FrozenClaimSupport_{identity}", __base__=ClaimSupportDraft,
            claim_id=(Literal[identities], ...) if identities else (str, ...),
        )
        return create_model(
            f"FrozenClaimSupportProposal_{identity}", __base__=ClaimSupportProposal,
            assessments=(tuple[assessment, ...], Field(min_length=len(identities), max_length=len(identities))),
        )


class ValidatedContextDossier(_SynthesisModel):
    dossier_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_spec_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    synthesizer_version: str = Field(min_length=1, max_length=64)
    sections: tuple[SynthesisSection, ...]
    claims: tuple[ContextSynthesisClaim, ...]
    unresolved_questions: tuple[str, ...]
    support_assessments: tuple[ClaimSupportAssessment, ...]


class ContextSynthesisReview(_SynthesisModel):
    work_spec_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    synthesizer_version: str = Field(min_length=1, max_length=64)
    draft: ContextSynthesisDraft = Field(repr=False)
    support_assessments: tuple[ClaimSupportAssessment, ...] | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def require_assessment_identity(self) -> Self:
        if self.support_assessments is not None:
            identities = tuple(item.claim_id for item in self.support_assessments)
            expected = {item.claim_id for item in self.draft.claims if item.authority == "confirmed"}
            if len(identities) != len(set(identities)) or set(identities) != expected:
                raise ValueError("synthesis review assessments 必须恰好覆盖候选 confirmed identities")
        return self


class ContextSynthesisResult(_SynthesisModel):
    dossier: ValidatedContextDossier | None = None
    blocker_code: str | None = None
    blocker_summary: str | None = None
    attempt_records: tuple[dict[str, Any], ...] = ()
    review: ContextSynthesisReview | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def require_result_shape(self) -> Self:
        blocked = self.blocker_code is not None or self.blocker_summary is not None
        if (self.dossier is None) != blocked:
            raise ValueError("synthesis result 必须恰好包含 dossier 或完整 blocker")
        if blocked and not (self.blocker_code and self.blocker_summary):
            raise ValueError("synthesis blocker 必须包含 code 与 summary")
        if self.review is not None and not blocked:
            raise ValueError("synthesis review 仅属于失败候选，不能替代有效 dossier")
        return self


class ContextSynthesizerPort(Protocol):
    VERSION: str

    async def synthesize(
        self,
        observation: LoopObservationEnvelope,
        work_spec: WorkContextSpec,
        bundle: ResolvedEvidenceBundle,
    ) -> ContextSynthesisResult: ...


class ContextSynthesisValidator:
    VERSION = "claim-graph-validator-v1"

    def validate(
        self,
        work_spec: WorkContextSpec,
        bundle: ResolvedEvidenceBundle,
        draft: ContextSynthesisDraft,
        support_assessments: tuple[ClaimSupportAssessment, ...],
        *,
        synthesizer_version: str,
    ) -> ValidatedContextDossier:
        self.validate_draft(work_spec, bundle, draft)
        assessments = self._validated_support(draft.claims, support_assessments)
        payload = (
            work_spec.work_spec_id,
            bundle.resolution_id,
            synthesizer_version,
            tuple(item.model_dump(mode="json") for item in draft.sections),
            tuple(item.model_dump(mode="json") for item in draft.claims),
            tuple(sorted(set(draft.unresolved_questions))),
            tuple(item.model_dump(mode="json") for item in assessments),
        )
        return ValidatedContextDossier(
            dossier_id=stable_expansion_hash("validated-context-dossier", *payload),
            work_spec_id=work_spec.work_spec_id,
            resolution_id=bundle.resolution_id,
            synthesizer_version=synthesizer_version,
            sections=draft.sections,
            claims=draft.claims,
            unresolved_questions=tuple(sorted(set(draft.unresolved_questions))),
            support_assessments=assessments,
        )

    @staticmethod
    def _validated_support(
        claims: tuple[ContextSynthesisClaim, ...],
        assessments: tuple[ClaimSupportAssessment, ...],
    ) -> tuple[ClaimSupportAssessment, ...]:
        by_claim = {item.claim_id: item for item in assessments}
        if len(by_claim) != len(assessments):
            raise ValueError("direct-support assessment claim identity 重复")
        confirmed = {item.claim_id: item for item in claims if item.authority == "confirmed"}
        if set(by_claim) != set(confirmed):
            raise ValueError("direct-support assessments 必须恰好覆盖全部 confirmed claims")
        failures = []
        for claim_id, claim in sorted(confirmed.items()):
            assessment = by_claim[claim_id]
            citation_keys = tuple(evidence_ref_key(item) for item in claim.citations)
            citation_match = tuple(assessment.citation_keys) == citation_keys
            if not citation_match or assessment.verdict != "supported":
                failures.append((claim_id, assessment.verdict, citation_match))
        if failures:
            first_id, first_verdict, first_match = failures[0]
            diagnostic = {
                "rejected_count": len(failures),
                "unsupported_count": sum(verdict == "unsupported" for _, verdict, _ in failures),
                "unknown_count": sum(verdict == "unknown" for _, verdict, _ in failures),
                "citation_mismatch_count": sum(not matched for _, _, matched in failures),
                "first_claim_id": first_id, "first_verdict": first_verdict, "first_citation_match": first_match,
            }
            raise ValueError("confirmed claim 未通过冻结 citation 的 direct-support 验证: " + json.dumps(diagnostic))
        return tuple(sorted(assessments, key=lambda item: item.claim_id))

    def validate_draft(
        self,
        work_spec: WorkContextSpec,
        bundle: ResolvedEvidenceBundle,
        draft: ContextSynthesisDraft,
    ) -> None:
        if bundle.work_spec_id != work_spec.work_spec_id:
            raise ValueError("synthesis inputs 的 WorkSpec identity 不一致")
        claims = {item.claim_id: item for item in draft.claims}
        if len(claims) != len(draft.claims):
            raise ValueError("synthesis claim identity 重复")
        section_claims = tuple(claim_id for section in draft.sections for claim_id in section.claim_ids)
        if set(section_claims) != set(claims) or len(section_claims) != len(set(section_claims)):
            raise ValueError("synthesis sections 必须恰好组织全部 claims")
        allowed_refs = {evidence_ref_key(item) for item in bundle.evidence_frontier}
        requirement_ids = {item.requirement_id for item in work_spec.evidence_requirements}
        question_ids = {self.question_id(question) for question in work_spec.questions}
        for claim in draft.claims:
            if {evidence_ref_key(item) for item in claim.citations} - allowed_refs:
                raise ValueError("synthesis claim 引用了 bundle 之外的 evidence")
            if set(claim.premise_claim_ids) - set(claims):
                raise ValueError("synthesis claim 引用了未知 premise")
            if set(claim.requirement_ids) - requirement_ids:
                raise ValueError("synthesis claim 引用了未知 requirement")
            if set(claim.question_ids) - question_ids:
                raise ValueError("synthesis claim 引用了未知 question")
        self._require_acyclic(claims)
        for claim in draft.claims:
            if claim.authority == "inference" and any(
                claims[premise].authority == "hypothesis"
                for premise in claim.premise_claim_ids
            ):
                raise ValueError("inference claim 不得把 hypothesis 当作已验证 premise")
        required = {
            item.requirement_id
            for item in work_spec.evidence_requirements
            if item.necessity == "required"
        }
        covered_requirements = {identity for claim in draft.claims for identity in claim.requirement_ids}
        if not required.issubset(covered_requirements):
            raise ValueError("synthesis 未覆盖 required evidence requirements")
        covered_questions = {identity for claim in draft.claims for identity in claim.question_ids}
        unresolved = {self.question_id(question) for question in draft.unresolved_questions}
        if not question_ids.issubset(covered_questions | unresolved):
            raise ValueError("synthesis 未覆盖 WorkSpec required questions")

    @staticmethod
    def question_id(question: str) -> str:
        return stable_expansion_hash("work-spec-question", " ".join(question.split()).casefold())

    @staticmethod
    def _require_acyclic(claims: dict[str, ContextSynthesisClaim]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(claim_id: str) -> None:
            if claim_id in visiting:
                raise ValueError("synthesis premise graph 包含循环")
            if claim_id in visited:
                return
            visiting.add(claim_id)
            for premise in claims[claim_id].premise_claim_ids:
                visit(premise)
            visiting.remove(claim_id)
            visited.add(claim_id)

        for claim_id in claims:
            visit(claim_id)


class WorkerResultContextSynthesizer:
    VERSION = "worker-context-synthesizer-v1"

    def __init__(self, validator: ContextSynthesisValidator | None = None) -> None:
        self._validator = validator or ContextSynthesisValidator()

    async def synthesize(
        self,
        observation: LoopObservationEnvelope,
        work_spec: WorkContextSpec,
        bundle: ResolvedEvidenceBundle,
    ) -> ContextSynthesisResult:
        synthesis = self._last_success(observation, "dossier_synthesizer")
        support = self._last_success(observation, "claim_verifier")
        if synthesis is None or support is None:
            return ContextSynthesisResult(
                blocker_code="synthesis_worker_missing",
                blocker_summary="缺少 dossier_synthesizer 或 claim_verifier 的受监督结果",
            )
        try:
            draft = ContextSynthesisDraft.model_validate(synthesis)
            assessments = TypeAdapter(tuple[ClaimSupportAssessment, ...]).validate_python(
                support.get("assessments") or ()
            )
            dossier = self._validator.validate(
                work_spec,
                bundle,
                draft,
                assessments,
                synthesizer_version=self.VERSION,
            )
        except (TypeError, ValidationError, ValueError) as exc:
            return ContextSynthesisResult(
                blocker_code="synthesis_invalid",
                blocker_summary=f"claim-level synthesis validation failed: {str(exc)[:1600]}",
            )
        return ContextSynthesisResult(dossier=dossier)

    @staticmethod
    def _last_success(observation: LoopObservationEnvelope, kind: str) -> dict[str, Any] | None:
        results = tuple(
            item.get("result") or {}
            for item in observation.worker_results
            if item.get("kind") == kind and item.get("status") in {"success", "proposed", "consumed"}
        )
        return results[-1] if results else None


def render_context_dossier(dossier: ValidatedContextDossier) -> str:
    claims = {item.claim_id: item for item in dossier.claims}
    lines = ["Evidence-grounded Context Dossier"]
    for section in dossier.sections:
        lines.append(f"\n## {section.title}")
        for claim_id in section.claim_ids:
            claim = claims[claim_id]
            citations = ", ".join("/".join(evidence_ref_key(item)) for item in claim.citations)
            suffix = f" [evidence: {citations}]" if citations else ""
            lines.append(f"- [{claim.authority}] {claim.statement}{suffix}")
    if dossier.unresolved_questions:
        lines.append("\n## Unresolved Questions")
        lines.extend(f"- {item}" for item in dossier.unresolved_questions)
    return "\n".join(lines)
