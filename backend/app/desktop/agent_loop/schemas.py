r"""本文件对外提供 Agent Loop API、observation、completion 与 Patrol decision 封闭判别联合。

输入为用户目标、grant、冻结版本、Patrol action 和 verifier evidence；输出为拒绝未知字段的不可变
合同。具体工作流为 action 依 discriminator 解析，envelope 绑定所有控制 revision，Kernel 只接受
PatrolDecisionIntent。示例：`intent = PatrolDecisionIntent.model_validate(payload)`。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from backend.app.desktop.context_curation.contracts import (
    CreateLanePlan,
    UpdateLanePlan,
)


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
    ContinueContextAction | CreateLaneAction | UpdateLaneAction | MergeContextsAction |
    PauseLaneAction | DiscardMembershipAction | RequestLaneCuratorAction |
    RequestCompletionVerifierAction | RequestCompletionAction | AdoptWorkspaceResultAction |
    WaitForUserAction | StopLoopAction,
    Field(discriminator="action"),
]
PATROL_ACTION_ADAPTER = TypeAdapter(PatrolAction)


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
    observed_frontier_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_workspace_revision: int = Field(gt=0)
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


class LoopCreateRequest(StrictModel):
    loop_id: str
    workspace_id: str
    initial_context_id: str
    holder_id: str
    goal: str = Field(min_length=1)
    task_contract: str = Field(min_length=1)
    acceptance_criteria: tuple[dict[str, Any], ...] = Field(min_length=1)
    capabilities: tuple[str, ...]
    context_scope: tuple[str, ...] = Field(min_length=1)
    permission_scope: tuple[str, ...]
    delegable_gates: tuple[str, ...] = ()
    budgets: LoopBudgetContract = Field(default_factory=LoopBudgetContract)
    equipment: dict[str, Any] = Field(default_factory=dict)
    expires_at: str | None = None


class LoopObservationEnvelope(StrictModel):
    loop_id: str
    loop_revision: int
    round_id: str
    goal_revision: int
    authority_revision: int
    observed_frontier_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    goal: dict[str, Any]
    grant: dict[str, Any]
    portfolio_frontier: tuple[dict[str, Any], ...]
    stable_results: tuple[dict[str, Any], ...] = ()
    workspace: dict[str, Any]
    budget: dict[str, Any]
    pending_decisions: tuple[dict[str, Any], ...] = ()
    worker_results: tuple[dict[str, Any], ...] = ()
    expansion_handles: tuple[dict[str, str], ...] = ()


class CriterionVerification(StrictModel):
    criterion_id: str
    status: Literal["satisfied", "unsatisfied", "unknown"]
    evidence: tuple[dict[str, Any], ...] = ()
    explanation: str


class CompletionVerificationContract(StrictModel):
    verification_id: str
    loop_id: str
    round_id: str
    goal_revision: int
    frontier_hash: str
    workspace_revision: int
    criteria: tuple[CriterionVerification, ...]
    conclusion: Literal["satisfied", "unsatisfied", "unknown"]
    unresolved: tuple[str, ...] = ()
