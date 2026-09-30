r"""本文件对外提供 Agent Loop API、版本化 Expansion 预算、Mission、类型化等待原因/响应、用户介入、observation、completion 与 Patrol decision 封闭判别联合。

输入为用户 Mission 或兼容旧目标、grant、冻结版本、Expansion/recovery opportunity、Patrol action 和 verifier evidence；输出为拒绝未知字段的不可变
合同。具体工作流为预算合同验证 Expansion 子策略并标记来源，创建请求再解析结构化 Mission 或无损适配旧三字段，介入请求区分 Context/Portfolio 作用域，模型只以 identity
选择（spawn_context/recover_context）或结构化拒绝（decline_expansion）表达 Context 派生与恢复、可信 plan 由服务端从冻结 opportunity 取用，持久 legacy create 仅由兼容 adapter 解析，其余 action 依 discriminator 解析，
envelope 绑定所有控制 revision 与未处理用户意图，completion 以稳定 check_id 绑定类型化证据；bootstrap intent 有模型外来源标识，自主压缩 action 只能引用已持久化候选，Kernel 只接受
PatrolDecisionIntent。示例：`intent = PatrolDecisionIntent.model_validate(payload)`。
新版 Observation 校验完整 TaskProgress、来源 manifest 和已提交 Lineage 合同；legacy 输入保留原 schema。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)

from backend.app.desktop.agent_loop.compression_authority.contracts import (
    ApplyContextCompressionAction,
    AutonomousCompressionPolicy,
)
from backend.app.desktop.agent_loop.expansion_resource_policy import (
    ExpansionResourcePolicy,
)
from backend.app.desktop.agent_loop.mission_contract import (
    EvidenceKind,
    LegacyMissionAdapter,
    LoopMissionContract,
)
from backend.app.desktop.agent_loop.task_progress.contracts import (
    TaskDeltaManifest,
    TaskProgressDocument,
)
from backend.app.desktop.context_curation.contracts import (
    CreateLanePlan,
    UpdateLanePlan,
)
from backend.app.desktop.context_evolution.lineage_contracts import LineageSnapshot


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContinueContextAction(StrictModel):
    action: Literal["continue_context"]
    context_id: str
    context_revision_id: str
    message: str = Field(min_length=1)
    required_barrier: bool = True


class CreateLaneAction(StrictModel):
    action: Literal["create_lane"]
    plan: CreateLanePlan
    message: str = Field(min_length=1)


class SpawnContextAction(StrictModel):
    action: Literal["spawn_context"]
    opportunity_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecoverContextAction(StrictModel):
    action: Literal["recover_context"]
    opportunity_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class DeclineExpansionAction(StrictModel):
    action: Literal["decline_expansion"]
    opportunity_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    blocker_code: Literal[
        "portfolio_projection_failed",
        "cognitive_planning_failed",
        "not_independent",
        "completion_not_decidable",
        "authority_missing",
        "source_out_of_scope",
        "source_unreadable",
        "required_evidence_unresolved",
        "evidence_budget_exhausted",
        "dossier_invalid",
        "context_budget_exhausted",
        "lane_budget_exhausted",
        "round_lane_budget_exhausted",
        "concurrency_budget_exhausted",
        "workspace_conflict",
        "workspace_isolation_unavailable",
        "duplicate_expansion",
        "stale_source",
        "compiler_failed",
    ]
    reason: str = Field(min_length=1, max_length=2000)


class UpdateLaneAction(StrictModel):
    action: Literal["update_lane"]
    plan: UpdateLanePlan
    message: str = Field(min_length=1)


class MergeContextsAction(StrictModel):
    action: Literal["merge_contexts"]
    plan: CreateLanePlan
    message: str = Field(min_length=1)


class PauseLaneAction(StrictModel):
    action: Literal["pause_lane"]
    lane_id: str


class DiscardMembershipAction(StrictModel):
    action: Literal["discard_membership"]
    membership_id: str


class RequestLaneCuratorAction(StrictModel):
    action: Literal["request_lane_curator"]
    assignments: tuple[dict[str, Any], ...] = Field(min_length=1)


class RequestCompletionVerifierAction(StrictModel):
    action: Literal["request_completion_verifier"]
    candidate_context_ids: tuple[str, ...] = Field(min_length=1)


class RequestCompletionAction(StrictModel):
    action: Literal["request_completion"]
    verification_id: str
    final_context_ids: tuple[str, ...] = Field(min_length=1)
    final_slot_id: str


class AdoptWorkspaceResultAction(StrictModel):
    action: Literal["adopt_workspace_result"]
    source_slot_id: str
    source_revision: int = Field(gt=0)
    rationale: str = Field(min_length=1, max_length=2000)


class WaitEvidenceIdentity(StrictModel):
    kind: Literal["mission", "gate", "capability", "budget", "external", "input"]
    reference_id: str = Field(min_length=1, max_length=160)
    revision: int | None = Field(default=None, gt=0)


class WaitForUserAction(StrictModel):
    action: Literal["wait_for_user"]
    reason: str = Field(min_length=1)
    cause: Literal["missing_goal", "missing_input", "human_gate", "permission", "budget", "external_blocker"] | None = None
    required_input: str | None = Field(default=None, min_length=1, max_length=1000)
    evidence_identity: WaitEvidenceIdentity | None = None


class StopLoopAction(StrictModel):
    action: Literal["stop_loop"]
    reason: str = Field(min_length=1)


PatrolAction = Annotated[
    ContinueContextAction | CreateLaneAction | SpawnContextAction | RecoverContextAction | DeclineExpansionAction | UpdateLaneAction | MergeContextsAction |
    PauseLaneAction | DiscardMembershipAction | RequestLaneCuratorAction |
    RequestCompletionVerifierAction | RequestCompletionAction | AdoptWorkspaceResultAction |
    ApplyContextCompressionAction | WaitForUserAction | StopLoopAction,
    Field(discriminator="action"),
]
PATROL_ACTION_ADAPTER = TypeAdapter(PatrolAction)

PatrolModelAction = Annotated[
    ContinueContextAction | SpawnContextAction | RecoverContextAction | DeclineExpansionAction | UpdateLaneAction | MergeContextsAction |
    PauseLaneAction | DiscardMembershipAction | RequestLaneCuratorAction |
    RequestCompletionVerifierAction | RequestCompletionAction | AdoptWorkspaceResultAction |
    ApplyContextCompressionAction | WaitForUserAction | StopLoopAction,
    Field(discriminator="action"),
]
PATROL_MODEL_ACTION_ADAPTER = TypeAdapter(PatrolModelAction)


class PatrolDecisionIntent(StrictModel):
    decision_id: str
    idempotency_key: str
    origin_kind: Literal["patrol", "mission_bootstrap"] = "patrol"
    loop_id: str
    loop_revision: int = Field(gt=0)
    round_id: str
    holder_id: str
    grant_id: str
    grant_revision: int = Field(gt=0)
    goal_revision: int = Field(gt=0)
    fencing_token: int = Field(default=0, ge=0)
    observed_frontier_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_workspace_revision: int = Field(gt=0)
    observed_projection_sequence: int = Field(default=0, ge=0)
    base_entity_revisions: dict[str, int | str] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=4000)
    evidence: tuple[dict[str, Any], ...] = ()
    actions: tuple[PatrolAction, ...] = Field(min_length=1)


class LoopBudgetContract(StrictModel):
    max_rounds: int = Field(default=50, ge=1, le=1000)
    max_duration_seconds: int = Field(default=86400, ge=60)
    max_model_calls: int = Field(default=200, ge=1)
    max_input_tokens: int = Field(default=2_000_000, ge=1)
    max_output_tokens: int = Field(default=500_000, ge=1)
    max_retries: int = Field(default=20, ge=0)
    max_lanes: int = Field(default=8, ge=1, le=64)
    max_contexts: int = Field(default=16, ge=1, le=256)
    max_providers: int = Field(default=4, ge=1, le=32)
    max_new_lanes_per_round: int = Field(default=3, ge=0, le=16)
    max_concurrent_runs: int = Field(default=4, ge=1, le=32)
    max_no_progress: int = Field(default=3, ge=1, le=20)
    expansion_resources: ExpansionResourcePolicy = Field(default_factory=ExpansionResourcePolicy)
    expansion_resources_source: Literal["default", "explicit"] = "default"

    def as_grant_budgets(self, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        budgets = self.model_dump(mode="json")
        if previous:
            for name in type(self).model_fields:
                if name not in self.model_fields_set and name in previous:
                    budgets[name] = previous[name]
        if "expansion_resources" in self.model_fields_set:
            budgets["expansion_resources_source"] = "explicit"
        elif previous and previous.get("expansion_resources"):
            budgets["expansion_resources"] = ExpansionResourcePolicy.model_validate(budgets["expansion_resources"]).model_dump(mode="json")
            budgets["expansion_resources_source"] = previous.get("expansion_resources_source", "explicit")
        else:
            budgets["expansion_resources_source"] = "default"
        return budgets


class NarrowLoopGrantRequest(StrictModel):
    command: Literal["narrow"]
    capabilities: tuple[str, ...] = Field(min_length=1)
    context_scope: tuple[str, ...] = Field(min_length=1)
    permission_scope: tuple[str, ...]
    delegable_gates: tuple[str, ...] = ()
    compression_policy: AutonomousCompressionPolicy | None = None
    expires_at: str | None = None


class AdjustLoopBudgetsRequest(StrictModel):
    command: Literal["adjust_budgets"]
    budgets: LoopBudgetContract


class RevokeLoopGrantRequest(StrictModel):
    command: Literal["revoke"]


LoopGrantMutationRequest = Annotated[
    NarrowLoopGrantRequest | AdjustLoopBudgetsRequest | RevokeLoopGrantRequest,
    Field(discriminator="command"),
]


class LoopCreateRequest(StrictModel):
    loop_id: str
    workspace_id: str
    initial_context_id: str
    initial_run_id: str
    readiness_token: str | None = Field(default=None, min_length=64, max_length=64)
    activation_key: str | None = Field(default=None, min_length=1, max_length=160)
    holder_id: str
    mission: LoopMissionContract | None = None
    goal: str | None = Field(default=None, min_length=1)
    task_contract: str | None = Field(default=None, min_length=1)
    acceptance_criteria: tuple[dict[str, Any], ...] | None = Field(default=None, min_length=1)
    capabilities: tuple[str, ...]
    context_scope: tuple[str, ...] = Field(min_length=1)
    permission_scope: tuple[str, ...]
    delegable_gates: tuple[str, ...] = ()
    compression_policy: AutonomousCompressionPolicy | None = None
    budgets: LoopBudgetContract = Field(default_factory=LoopBudgetContract)
    equipment: dict[str, Any] = Field(default_factory=dict)
    expires_at: str | None = None

    @model_validator(mode="after")
    def require_one_mission_shape(self) -> LoopCreateRequest:
        legacy = (self.goal, self.task_contract, self.acceptance_criteria)
        if self.mission is not None and any(value is not None for value in legacy):
            raise ValueError("mission 与旧 goal/task_contract/acceptance_criteria 不能同时提交")
        if self.mission is None and any(value is None for value in legacy):
            raise ValueError("必须提交 mission 或完整旧目标字段")
        return self

    def resolved_mission(self) -> LoopMissionContract:
        if self.mission is not None:
            return self.mission
        return LegacyMissionAdapter.convert(
            goal=self.goal or "",
            task_contract=self.task_contract or "",
            acceptance_criteria=self.acceptance_criteria or (),
        )


class LoopInterventionRequest(StrictModel):
    mode: Literal["patrol_context_intent", "patrol_portfolio_intent"]
    content: str = Field(min_length=1, max_length=12000)
    context_id: str | None = None

    def scope(self) -> str:
        return "context" if self.mode == "patrol_context_intent" else "portfolio"


class LoopWaitResponseRequest(StrictModel):
    request_revision: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=160)
    answer: dict[str, Any]


class ResumeWithCurrentMissionRequest(StrictModel):
    confirmation: Literal["resume_with_current_mission"]
    request_revision: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=160)


class LoopObservationEnvelope(StrictModel):
    input_schema_version: Literal[0, 1] = 0
    decision_inputs_ref: str | None = None
    previous_task_progress: dict[str, Any] | None = None
    task_delta: dict[str, Any] | None = None
    committed_lineage: dict[str, Any] | None = None
    loop_id: str
    loop_revision: int
    round_id: str
    goal_revision: int
    authority_revision: int
    observed_frontier_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_sequence: int = Field(default=0, ge=0)
    base_entity_revisions: dict[str, int | str] = Field(default_factory=dict)
    mission: dict[str, Any] | None = None
    goal: dict[str, Any] | None = None
    grant: dict[str, Any]
    portfolio_frontier: tuple[dict[str, Any], ...]
    stable_results: tuple[dict[str, Any], ...] = ()
    workspace: dict[str, Any]
    budget: dict[str, Any]
    pending_decisions: tuple[dict[str, Any], ...] = ()
    worker_results: tuple[dict[str, Any], ...] = ()
    user_intents: tuple[dict[str, Any], ...] = ()
    expansion_handles: tuple[dict[str, str], ...] = ()
    expansion_assessment: dict[str, Any] | None = None
    recovery_opportunities: tuple[dict[str, Any], ...] = ()
    recovery_waiting_reason: str | None = None

    @model_validator(mode="after")
    def require_mission_or_legacy_goal(self) -> LoopObservationEnvelope:
        if self.mission is None and self.goal is None:
            raise ValueError("observation 必须包含结构化 Mission 或旧 Goal")
        if self.input_schema_version == 1:
            if not self.decision_inputs_ref or self.previous_task_progress is None or self.task_delta is None or self.committed_lineage is None:
                raise ValueError("新版 Observation 必须完整绑定三项冻结输入")
            TaskProgressDocument.model_validate(self.previous_task_progress)
            manifest = TaskDeltaManifest.model_validate(self.task_delta)
            if not manifest.complete:
                raise ValueError("新版 Observation 不接受未完成的来源枚举")
            LineageSnapshot.model_validate(self.committed_lineage)
        return self


class CompletionEvidenceReference(StrictModel):
    kind: EvidenceKind
    source_id: str = Field(min_length=1, max_length=240)
    summary: str | None = Field(default=None, max_length=2000)


class CriterionVerification(StrictModel):
    check_id: str = Field(validation_alias=AliasChoices("check_id", "criterion_id"), min_length=1, max_length=96)
    status: Literal["satisfied", "unsatisfied", "unknown"]
    evidence: tuple[CompletionEvidenceReference, ...] = ()
    explanation: str

    @model_validator(mode="after")
    def require_evidence_for_satisfied_check(self) -> CriterionVerification:
        if self.status == "satisfied" and not self.evidence:
            raise ValueError("satisfied 完成检查必须包含类型化证据")
        return self


class CompletionVerificationContract(StrictModel):
    verification_id: str
    loop_id: str
    round_id: str
    goal_revision: int
    frontier_hash: str
    workspace_revision: int
    criteria: tuple[CriterionVerification, ...] = Field(min_length=1)
    conclusion: Literal["satisfied", "unsatisfied", "unknown"]
    unresolved: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_unique_declared_check_ids(self) -> CompletionVerificationContract:
        check_ids = [item.check_id for item in self.criteria]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("Completion verification check_id 必须唯一")
        if not set(self.unresolved).issubset(check_ids):
            raise ValueError("unresolved 只能引用本 verification 的 check_id")
        return self
