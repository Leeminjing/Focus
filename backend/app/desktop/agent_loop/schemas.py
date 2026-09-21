r"""本文件对外提供 Agent Loop API、Mission、类型化等待响应、用户介入、observation、completion 与 Patrol decision 封闭判别联合。

输入为用户 Mission 或兼容旧目标、grant、冻结版本、Expansion assessment、Patrol action 和 verifier evidence；输出为拒绝未知字段的不可变
合同。具体工作流为创建请求先解析结构化 Mission 或无损适配旧三字段，介入请求区分 Context/Portfolio 作用域，模型只以 identity
选择（spawn_context）或结构化拒绝（decline_expansion）表达 Context 派生、语义字段由服务端从冻结 opportunity 取用，持久 legacy create 仅由兼容 adapter 解析，其余 action 依 discriminator 解析，
envelope 绑定所有控制 revision 与未处理用户意图，completion 以稳定 check_id 绑定类型化证据；自主压缩 action 只能引用已持久化候选，Kernel 只接受
PatrolDecisionIntent。示例：`intent = PatrolDecisionIntent.model_validate(payload)`。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from backend.app.desktop.context_curation.contracts import (
    CreateLanePlan,
    UpdateLanePlan,
)
from backend.app.desktop.agent_loop.compression_authority.contracts import (
    ApplyContextCompressionAction,
    AutonomousCompressionPolicy,
)
from backend.app.desktop.agent_loop.mission_contract import EvidenceKind, LegacyMissionAdapter, LoopMissionContract


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


class DeclineExpansionAction(StrictModel):
    action: Literal["decline_expansion"]
    opportunity_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    blocker_code: Literal[
        "not_independent",
        "authority_missing",
        "source_out_of_scope",
        "source_unreadable",
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


class WaitForUserAction(StrictModel):
    action: Literal["wait_for_user"]
    reason: str = Field(min_length=1)


class StopLoopAction(StrictModel):
    action: Literal["stop_loop"]
    reason: str = Field(min_length=1)


PatrolAction = Annotated[
    ContinueContextAction | CreateLaneAction | SpawnContextAction | DeclineExpansionAction | UpdateLaneAction | MergeContextsAction |
    PauseLaneAction | DiscardMembershipAction | RequestLaneCuratorAction |
    RequestCompletionVerifierAction | RequestCompletionAction | AdoptWorkspaceResultAction |
    ApplyContextCompressionAction | WaitForUserAction | StopLoopAction,
    Field(discriminator="action"),
]
PATROL_ACTION_ADAPTER = TypeAdapter(PatrolAction)

PatrolModelAction = Annotated[
    ContinueContextAction | SpawnContextAction | DeclineExpansionAction | UpdateLaneAction | MergeContextsAction |
    PauseLaneAction | DiscardMembershipAction | RequestLaneCuratorAction |
    RequestCompletionVerifierAction | RequestCompletionAction | AdoptWorkspaceResultAction |
    ApplyContextCompressionAction | WaitForUserAction | StopLoopAction,
    Field(discriminator="action"),
]
PATROL_MODEL_ACTION_ADAPTER = TypeAdapter(PatrolModelAction)


class PatrolDecisionIntent(StrictModel):
    decision_id: str
    idempotency_key: str
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
    def require_one_mission_shape(self) -> "LoopCreateRequest":
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


class LoopObservationEnvelope(StrictModel):
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

    @model_validator(mode="after")
    def require_mission_or_legacy_goal(self) -> "LoopObservationEnvelope":
        if self.mission is None and self.goal is None:
            raise ValueError("observation 必须包含结构化 Mission 或旧 Goal")
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
    def require_evidence_for_satisfied_check(self) -> "CriterionVerification":
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
    def require_unique_declared_check_ids(self) -> "CompletionVerificationContract":
        check_ids = [item.check_id for item in self.criteria]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("Completion verification check_id 必须唯一")
        if not set(self.unresolved).issubset(check_ids):
            raise ValueError("unresolved 只能引用本 verification 的 check_id")
        return self
