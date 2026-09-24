r"""本文件对外提供 Context expansion 的工作规格、语义证据、阶段结果与稳定 identity 合同。

输入为冻结 Loop identity、认知工作目标、证据需求、语义 manifest、精确证据 frontier 与 Workspace 需求；输出为
`WorkContextSpec`、`ContextSemanticManifest`、`ResolvedEvidenceBundle`、opportunity、assessment、intent 与 outcome。
具体工作流为把 identity-bearing 数据规范化为深度不可变结构，对 canonical JSON 求哈希，并验证 confirmed statement、
required coverage 与引用 frontier，使规划、解析、重放和提交共享同一语义身份。示例：`spec = WorkContextSpec.create(...)`。
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.desktop.context_curation import (
    CreateLanePlan,
    EvidenceRef,
    MultiSourceEvidence,
    evidence_ref_key,
)
from backend.app.desktop.context_evolution import ContextRevisionRef

WorkspaceMode = Literal["read_only", "isolated_write"]
EvidenceNecessity = Literal["required", "optional"]
EvidenceRole = Literal[
    "requirement",
    "implementation",
    "test",
    "failure",
    "decision",
    "material",
    "workspace_effect",
    "conversation",
]
SemanticAuthority = Literal["confirmed", "hypothesis"]
SemanticUnitKind = Literal[
    "decision",
    "claim",
    "hypothesis",
    "unresolved_question",
    "implementation_effect",
    "verification_result",
    "failure",
]
DerivationStage = Literal[
    "signal_collection",
    "portfolio_indexing",
    "portfolio_projection",
    "retrieval_planning",
    "cognitive_planning",
    "work_reconciliation",
    "admission",
    "evidence_resolution",
    "dossier_synthesis",
    "context_quality",
    "compilation",
    "shadow_comparison",
]
ExpansionLevel = Literal["required", "recommended", "not_applicable"]
ExpansionBlockerCode = Literal[
    "portfolio_projection_failed",
    "portfolio_index_failed",
    "portfolio_catalog_overflow",
    "retrieval_budget_exhausted",
    "retrieval_session_stale",
    "cognitive_planning_failed",
    "work_reconciliation_failed",
    "work_spec_conflict",
    "not_independent",
    "completion_not_decidable",
    "authority_missing",
    "source_out_of_scope",
    "source_unreadable",
    "required_evidence_unresolved",
    "evidence_budget_exhausted",
    "dossier_invalid",
    "synthesis_worker_missing",
    "synthesis_invalid",
    "quality_preflight_failed",
    "quality_worker_missing",
    "quality_contract_invalid",
    "context_quality_failed",
    "context_budget_exhausted",
    "lane_budget_exhausted",
    "round_lane_budget_exhausted",
    "concurrency_budget_exhausted",
    "workspace_conflict",
    "workspace_isolation_unavailable",
    "duplicate_expansion",
    "stale_source",
    "compiler_failed",
    "automatic_expansion_disabled",
]
ExpansionLifecycleState = Literal[
    "signals_collected",
    "indexes_ready",
    "portfolio_projected",
    "retrieval_planned",
    "work_planned",
    "work_reconciled",
    "admitted",
    "proposed",
    "evidence_resolved",
    "dossier_built",
    "synthesis_omitted",
    "quality_verified",
    "compiled",
    "authorized",
    "committed",
    "dispatched",
    "declined",
    "blocked",
    "failed",
    "superseded",
]


class _ExpansionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def stable_expansion_hash(namespace: str, *parts: Any) -> str:
    payload = json.dumps(
        {"namespace": namespace, "parts": parts},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _normalized(value: str) -> str:
    return " ".join(value.split()).strip()


class EvidenceSourceConstraints(_ExpansionModel):
    context_roles: tuple[str, ...] = ()
    evidence_kinds: tuple[str, ...] = ()
    context_ids: tuple[str, ...] = ()

    @field_validator("context_roles", "evidence_kinds", "context_ids", mode="before")
    @classmethod
    def normalize_items(cls, values: Any) -> tuple[str, ...]:
        return tuple(sorted({_normalized(str(item)).casefold() for item in (values or ()) if _normalized(str(item))}))


class EvidenceRequirement(_ExpansionModel):
    requirement_id: str = Field(min_length=1, max_length=120)
    role: EvidenceRole
    question: str = Field(min_length=1, max_length=2000)
    coverage_criterion: str = Field(min_length=1, max_length=2000)
    necessity: EvidenceNecessity = "required"
    source_constraints: EvidenceSourceConstraints = Field(default_factory=EvidenceSourceConstraints)
    candidate_unit_ids: tuple[str, ...] = ()

    @field_validator("requirement_id", "question", "coverage_criterion", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return _normalized(str(value or ""))

    @field_validator("candidate_unit_ids", mode="before")
    @classmethod
    def normalize_candidates(cls, values: Any) -> tuple[str, ...]:
        return tuple(sorted({_normalized(str(item)) for item in (values or ()) if _normalized(str(item))}))

    def identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class WorkContextSpec(_ExpansionModel):
    work_spec_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    planner_version: str = Field(min_length=1, max_length=64)
    objective: str = Field(min_length=1, max_length=2000)
    separation_reason: str = Field(min_length=1, max_length=2000)
    questions: tuple[str, ...] = Field(min_length=1)
    completion_criteria: tuple[str, ...] = Field(min_length=1)
    workspace_requirement: WorkspaceMode
    evidence_requirements: tuple[EvidenceRequirement, ...] = Field(min_length=1)

    @field_validator("planner_version", "objective", "separation_reason", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return _normalized(str(value or ""))

    @field_validator("questions", "completion_criteria", mode="before")
    @classmethod
    def normalize_statements(cls, values: Any) -> tuple[str, ...]:
        return tuple(sorted({_normalized(str(item)) for item in (values or ()) if _normalized(str(item))}))

    @field_validator("evidence_requirements", mode="before")
    @classmethod
    def order_requirements(cls, values: Any) -> tuple[Any, ...]:
        parsed = tuple(values or ())
        return tuple(sorted(parsed, key=lambda item: str(item.get("requirement_id") if isinstance(item, dict) else item.requirement_id)))

    @model_validator(mode="after")
    def require_stable_identity(self) -> Self:
        ids = [item.requirement_id for item in self.evidence_requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("work spec evidence requirement identity 重复")
        if self.work_spec_id != self._identity():
            raise ValueError("work spec identity 与规范化语义不一致")
        return self

    def _identity(self) -> str:
        return stable_expansion_hash(
            "work-context-spec",
            self.planner_version,
            self.objective.casefold(),
            self.separation_reason.casefold(),
            tuple(item.casefold() for item in self.questions),
            tuple(item.casefold() for item in self.completion_criteria),
            self.workspace_requirement,
            tuple(item.identity_payload() for item in self.evidence_requirements),
        )

    @classmethod
    def create(
        cls,
        *,
        planner_version: str,
        objective: str,
        separation_reason: str,
        questions: tuple[str, ...],
        completion_criteria: tuple[str, ...],
        workspace_requirement: WorkspaceMode,
        evidence_requirements: tuple[EvidenceRequirement, ...],
    ) -> Self:
        normalized_questions = tuple(sorted({_normalized(item) for item in questions if _normalized(item)}))
        normalized_completion = tuple(sorted({_normalized(item) for item in completion_criteria if _normalized(item)}))
        ordered_requirements = tuple(sorted(evidence_requirements, key=lambda item: item.requirement_id))
        identity = stable_expansion_hash(
            "work-context-spec",
            _normalized(planner_version),
            _normalized(objective).casefold(),
            _normalized(separation_reason).casefold(),
            tuple(item.casefold() for item in normalized_questions),
            tuple(item.casefold() for item in normalized_completion),
            workspace_requirement,
            tuple(item.identity_payload() for item in ordered_requirements),
        )
        return cls(
            work_spec_id=identity,
            planner_version=planner_version,
            objective=objective,
            separation_reason=separation_reason,
            questions=normalized_questions,
            completion_criteria=normalized_completion,
            workspace_requirement=workspace_requirement,
            evidence_requirements=ordered_requirements,
        )


class WorkContextDraft(_ExpansionModel):
    objective: str = Field(min_length=1, max_length=2000)
    separation_reason: str = Field(min_length=1, max_length=2000)
    questions: tuple[str, ...] = Field(min_length=1)
    completion_criteria: tuple[str, ...] = Field(min_length=1)
    workspace_requirement: WorkspaceMode
    evidence_requirements: tuple[EvidenceRequirement, ...] = Field(min_length=1)
    required: bool = False

    def freeze(self, planner_version: str) -> WorkContextSpec:
        return WorkContextSpec.create(
            planner_version=planner_version,
            objective=self.objective,
            separation_reason=self.separation_reason,
            questions=self.questions,
            completion_criteria=self.completion_criteria,
            workspace_requirement=self.workspace_requirement,
            evidence_requirements=self.evidence_requirements,
        )


class SemanticEvidenceUnit(_ExpansionModel):
    unit_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: SemanticUnitKind
    authority: SemanticAuthority
    statement: str = Field(min_length=1, max_length=4000)
    evidence_refs: tuple[EvidenceRef, ...] = ()

    @field_validator("statement", mode="before")
    @classmethod
    def normalize_statement(cls, value: Any) -> str:
        return _normalized(str(value or ""))

    @model_validator(mode="after")
    def require_supported_confirmation(self) -> Self:
        keys = [evidence_ref_key(ref) for ref in self.evidence_refs]
        if len(keys) != len(set(keys)):
            raise ValueError("semantic unit evidence ref 重复")
        if self.authority == "confirmed" and not keys:
            raise ValueError("confirmed semantic unit 必须包含 evidence ref")
        expected = stable_expansion_hash(
            "semantic-evidence-unit",
            self.kind,
            self.authority,
            self.statement.casefold(),
            tuple(sorted(keys)),
        )
        if self.unit_id != expected:
            raise ValueError("semantic unit identity 与内容不一致")
        return self

    @classmethod
    def create(
        cls,
        *,
        kind: SemanticUnitKind,
        authority: SemanticAuthority,
        statement: str,
        evidence_refs: tuple[EvidenceRef, ...] = (),
    ) -> Self:
        normalized = _normalized(statement)
        ordered = tuple(sorted(evidence_refs, key=evidence_ref_key))
        return cls(
            unit_id=stable_expansion_hash(
                "semantic-evidence-unit",
                kind,
                authority,
                normalized.casefold(),
                tuple(evidence_ref_key(ref) for ref in ordered),
            ),
            kind=kind,
            authority=authority,
            statement=normalized,
            evidence_refs=ordered,
        )


class ContextSemanticManifest(_ExpansionModel):
    manifest_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: ContextRevisionRef
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    projector_version: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=120)
    active_objective: str = Field(min_length=1, max_length=2000)
    units: tuple[SemanticEvidenceUnit, ...] = ()

    @model_validator(mode="after")
    def require_stable_identity(self) -> Self:
        if not self.source.is_runnable:
            raise ValueError("semantic manifest source 必须可运行")
        ids = [item.unit_id for item in self.units]
        if len(ids) != len(set(ids)):
            raise ValueError("semantic manifest unit identity 重复")
        expected = stable_expansion_hash(
            "context-semantic-manifest",
            self.source.model_dump(mode="json"),
            self.source_content_hash,
            self.projector_version,
            tuple(ids),
        )
        if self.manifest_id != expected:
            raise ValueError("semantic manifest identity 与冻结来源不一致")
        return self

    @classmethod
    def create(
        cls,
        *,
        source: ContextRevisionRef,
        source_content_hash: str,
        projector_version: str,
        role: str,
        active_objective: str,
        units: tuple[SemanticEvidenceUnit, ...],
    ) -> Self:
        ordered = tuple(sorted(units, key=lambda item: item.unit_id))
        return cls(
            manifest_id=stable_expansion_hash(
                "context-semantic-manifest",
                source.model_dump(mode="json"),
                source_content_hash,
                projector_version,
                tuple(item.unit_id for item in ordered),
            ),
            source=source,
            source_content_hash=source_content_hash,
            projector_version=projector_version,
            role=role,
            active_objective=active_objective,
            units=ordered,
        )


class ResolvedEvidenceItem(_ExpansionModel):
    requirement_id: str = Field(min_length=1, max_length=120)
    ref: EvidenceRef
    content_hash: str = Field(min_length=64, max_length=64)
    relevance_reason: str = Field(min_length=1, max_length=2000)

    @field_validator("requirement_id", "relevance_reason", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return _normalized(str(value or ""))


class ResolvedEvidenceBundle(_ExpansionModel):
    resolution_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_spec_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_frontier: tuple[ContextRevisionRef, ...] = Field(min_length=1)
    evidence_frontier: tuple[EvidenceRef, ...] = ()
    items: tuple[ResolvedEvidenceItem, ...] = ()
    required_requirement_ids: tuple[str, ...] = ()
    evidence: MultiSourceEvidence

    @model_validator(mode="after")
    def require_complete_coverage(self) -> Self:
        source_ids = tuple(source.revision_id for source in self.source_frontier)
        evidence_source_ids = tuple(source.source.revision_id for source in self.evidence.sources)
        if source_ids != evidence_source_ids:
            raise ValueError("resolved bundle source frontier 与 evidence 不一致")
        frontier_keys = {evidence_ref_key(ref) for ref in self.evidence_frontier}
        if frontier_keys != {evidence_ref_key(ref) for ref in self.evidence.evidence_frontier}:
            raise ValueError("resolved bundle evidence frontier 与 evidence 不一致")
        if any(evidence_ref_key(item.ref) not in frontier_keys for item in self.items):
            raise ValueError("resolved item 引用了 evidence frontier 之外的证据")
        covered = {item.requirement_id for item in self.items}
        if not set(self.required_requirement_ids).issubset(covered):
            raise ValueError("required evidence requirement 未全部满足")
        if self.resolution_id != self._identity():
            raise ValueError("resolution identity 与精确证据不一致")
        return self

    def _identity(self) -> str:
        return stable_expansion_hash(
            "resolved-evidence",
            self.work_spec_id,
            tuple(source.model_dump(mode="json") for source in self.source_frontier),
            tuple(evidence_ref_key(ref) for ref in self.evidence_frontier),
            tuple(item.model_dump(mode="json") for item in self.items),
        )

    @classmethod
    def create(
        cls,
        *,
        work_spec: WorkContextSpec,
        evidence: MultiSourceEvidence,
        items: tuple[ResolvedEvidenceItem, ...],
    ) -> Self:
        ordered_sources = tuple(sorted(evidence.sources, key=lambda item: item.source.revision_id))
        evidence = evidence.model_copy(update={"sources": ordered_sources})
        source_frontier = tuple(source.source for source in ordered_sources)
        evidence_frontier = tuple(sorted(evidence.evidence_frontier, key=evidence_ref_key))
        ordered_items = tuple(sorted(items, key=lambda item: (item.requirement_id, evidence_ref_key(item.ref))))
        required = tuple(
            item.requirement_id
            for item in work_spec.evidence_requirements
            if item.necessity == "required"
        )
        identity = stable_expansion_hash(
            "resolved-evidence",
            work_spec.work_spec_id,
            tuple(source.model_dump(mode="json") for source in source_frontier),
            tuple(evidence_ref_key(ref) for ref in evidence_frontier),
            tuple(item.model_dump(mode="json") for item in ordered_items),
        )
        return cls(
            resolution_id=identity,
            work_spec_id=work_spec.work_spec_id,
            source_frontier=source_frontier,
            evidence_frontier=evidence_frontier,
            items=ordered_items,
            required_requirement_ids=required,
            evidence=evidence,
        )


class DerivationStageFailure(_ExpansionModel):
    stage: DerivationStage
    code: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2000)
    retryable: bool = False


class DerivationStageRecord(_ExpansionModel):
    stage: DerivationStage
    input_identities: tuple[str, ...] = ()
    output_identities: tuple[str, ...] = ()
    version: str = Field(min_length=1, max_length=120)
    duration_ms: float = Field(ge=0)
    safe_summary: str = Field(min_length=1, max_length=1000)
    failure_code: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("input_identities", "output_identities", mode="before")
    @classmethod
    def normalize_identities(cls, values: Any) -> tuple[str, ...]:
        return tuple(sorted({_normalized(str(item)) for item in (values or ()) if _normalized(str(item))}))


class CognitivePlanResult(_ExpansionModel):
    work_specs: tuple[WorkContextSpec, ...] = ()
    required_work_spec_ids: tuple[str, ...] = ()
    failure: DerivationStageFailure | None = None

    @model_validator(mode="after")
    def require_one_result_shape(self) -> Self:
        if self.failure is not None and self.work_specs:
            raise ValueError("planning failure 不得同时携带 work specs")
        known = {item.work_spec_id for item in self.work_specs}
        if not set(self.required_work_spec_ids).issubset(known):
            raise ValueError("required work spec identity 不在 planning result 中")
        return self


class ManifestProjectionResult(_ExpansionModel):
    manifests: tuple[ContextSemanticManifest, ...] = ()
    failure: DerivationStageFailure | None = None

    @model_validator(mode="after")
    def require_one_result_shape(self) -> Self:
        if self.failure is not None and self.manifests:
            raise ValueError("projection failure 不得同时携带 manifests")
        return self


class ExpansionOpportunity(_ExpansionModel):
    opportunity_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    loop_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_spec: WorkContextSpec
    manifest_sources: tuple[ContextRevisionRef, ...] = ()
    manifest_ids: tuple[str, ...] = ()
    projector_versions: tuple[str, ...] = ()
    signal_ids: tuple[str, ...] = ()
    required: bool = False

    @model_validator(mode="after")
    def require_stable_identity(self) -> Self:
        if any(not source.is_runnable for source in self.manifest_sources):
            raise ValueError("expansion opportunity 的 manifest sources 必须绑定可运行 Revision")
        source_ids = [source.revision_id for source in self.manifest_sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("expansion opportunity 的 manifest sources 重复")
        if len(self.manifest_ids) != len(set(self.manifest_ids)):
            raise ValueError("expansion opportunity 的 manifest identity 重复")
        if self.opportunity_id != self._identity():
            raise ValueError("expansion opportunity identity 与冻结工作规格不一致")
        return self

    @field_validator("manifest_sources", mode="before")
    @classmethod
    def order_sources(cls, values: Any) -> tuple[Any, ...]:
        return tuple(
            sorted(
                values or (),
                key=lambda item: str(
                    item.get("revision_id")
                    if isinstance(item, dict)
                    else item.revision_id
                ),
            )
        )

    @field_validator("manifest_ids", "projector_versions", "signal_ids", mode="before")
    @classmethod
    def order_identities(cls, values: Any) -> tuple[str, ...]:
        return tuple(sorted({_normalized(str(item)) for item in (values or ()) if _normalized(str(item))}))

    @property
    def independence_key(self) -> str:
        return self.work_spec.work_spec_id

    @property
    def semantic_fingerprint(self) -> str:
        return self.work_spec.work_spec_id

    def _identity(self) -> str:
        return stable_expansion_hash(
            "expansion-opportunity-v2",
            self.loop_id,
            self.round_id,
            self.observation_hash,
            self.work_spec.planner_version,
            self.work_spec.work_spec_id,
        )

    @classmethod
    def create(
        cls,
        *,
        loop_id: str,
        round_id: str,
        observation_hash: str,
        work_spec: WorkContextSpec,
        manifest_sources: tuple[ContextRevisionRef, ...] = (),
        manifest_ids: tuple[str, ...] = (),
        projector_versions: tuple[str, ...] = (),
        signal_ids: tuple[str, ...] = (),
        required: bool = False,
    ) -> Self:
        ordered_sources = tuple(sorted(manifest_sources, key=lambda item: item.revision_id))
        ordered_manifests = tuple(sorted(set(manifest_ids)))
        ordered_projectors = tuple(sorted(set(projector_versions)))
        ordered_signals = tuple(sorted(set(signal_ids)))
        identity = stable_expansion_hash(
            "expansion-opportunity-v2",
            loop_id,
            round_id,
            observation_hash,
            work_spec.planner_version,
            work_spec.work_spec_id,
        )
        return cls(
            opportunity_id=identity,
            loop_id=loop_id,
            round_id=round_id,
            observation_hash=observation_hash,
            work_spec=work_spec,
            manifest_sources=ordered_sources,
            manifest_ids=ordered_manifests,
            projector_versions=ordered_projectors,
            signal_ids=ordered_signals,
            required=required,
        )


class ExpansionBlocker(_ExpansionModel):
    code: ExpansionBlockerCode
    summary: str = Field(min_length=1, max_length=2000)
    opportunity_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    retryable: bool = False
    stage_records: tuple[DerivationStageRecord, ...] = ()


class ExpansionAssessment(_ExpansionModel):
    loop_id: str
    round_id: str
    frontier_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=64)
    level: ExpansionLevel
    opportunities: tuple[ExpansionOpportunity, ...] = ()
    blockers: tuple[ExpansionBlocker, ...] = ()
    decision_deadline_round: int | None = Field(default=None, ge=1)
    stage_records: tuple[DerivationStageRecord, ...] = ()
    reconciliation: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_consistent_level(self) -> Self:
        if self.level == "required" and not self.opportunities:
            raise ValueError("required assessment 必须包含 opportunity")
        if self.level == "not_applicable" and self.opportunities and not self.blockers:
            raise ValueError("带候选的 not_applicable assessment 必须解释 blocker")
        return self

    @property
    def requires_decision(self) -> bool:
        return self.level == "required"


class SpawnContextIntent(_ExpansionModel):
    action: Literal["spawn_context"] = "spawn_context"
    opportunity_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class DeclineExpansionIntent(_ExpansionModel):
    action: Literal["decline_expansion"] = "decline_expansion"
    opportunity_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    blocker_code: ExpansionBlockerCode
    reason: str = Field(min_length=1, max_length=2000)


ExpansionDecision = Annotated[SpawnContextIntent | DeclineExpansionIntent, Field(discriminator="action")]


class CompiledExpansion(_ExpansionModel):
    expansion_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    opportunity: ExpansionOpportunity
    intent: SpawnContextIntent
    resolution: ResolvedEvidenceBundle
    plan: CreateLanePlan
    definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler_version: str = Field(min_length=1, max_length=64)
    dossier_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    synthesis_omitted: bool = False
    quality_assessment_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    dossier_payload: dict[str, Any] | None = None
    quality_assessment_payload: dict[str, Any] | None = None
    stage_records: tuple[DerivationStageRecord, ...] = ()


class ExpansionOutcome(_ExpansionModel):
    expansion_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: ExpansionLifecycleState
    reason_code: ExpansionBlockerCode | None = None
    lane_id: str | None = None
    context_id: str | None = None
    context_revision_id: str | None = None
    directive_id: str | None = None
    run_id: str | None = None
