r"""本文件对外提供 Context expansion 的不可变语义合同与稳定 identity 函数。

输入为 Loop/round、精确来源 Revision、派生目的、工作指令、完成检查和工作区模式；输出为 opportunity、assessment、
spawn/decline intent、编译结果、blocker 与 outcome。具体工作流为规范化语义字段并对其 canonical JSON 求哈希，
使模型重试、进程重启和幂等提交共享同一 identity；派生意图只承载冻结 opportunity 的 identity，语义字段一律由
调用方从 opportunity 读取，编译结果因此不可能被模型改写。示例：`opportunity = ExpansionOpportunity.create(...)`。
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.desktop.context_curation import CreateLanePlan
from backend.app.desktop.context_evolution import ContextRevisionRef


WorkspaceMode = Literal["read_only", "isolated_write"]
ExpansionLevel = Literal["required", "recommended", "not_applicable"]
ExpansionTrigger = Literal[
    "mission_checks",
    "independent_verification",
    "repeated_failure",
    "token_pressure",
    "user_parallel_intent",
    "workspace_state",
    "curator_proposal",
]
ExpansionBlockerCode = Literal[
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
ExpansionLifecycleState = Literal[
    "detected",
    "curated",
    "proposed",
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


class ExpansionOpportunity(_ExpansionModel):
    opportunity_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    loop_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    source: ContextRevisionRef
    purpose: str = Field(min_length=1, max_length=1000)
    work_order: str = Field(min_length=1, max_length=8000)
    completion_check: str = Field(min_length=1, max_length=4000)
    workspace_mode: WorkspaceMode
    independence_key: str = Field(min_length=1, max_length=240)
    semantic_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    triggers: tuple[ExpansionTrigger, ...] = Field(min_length=1)
    evidence_hints: tuple[str, ...] = ()
    required: bool = False

    @model_validator(mode="after")
    def require_frozen_runnable_source(self) -> Self:
        if not self.source.is_runnable:
            raise ValueError("expansion opportunity 必须绑定可运行的精确来源 Revision")
        return self

    @field_validator("purpose", "work_order", "completion_check", "independence_key", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return _normalized(str(value or ""))

    @field_validator("triggers", "evidence_hints", mode="before")
    @classmethod
    def unique_items(cls, values: Any) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(item) for item in (values or ())))

    @classmethod
    def create(
        cls,
        *,
        loop_id: str,
        round_id: str,
        source: ContextRevisionRef,
        purpose: str,
        work_order: str,
        completion_check: str,
        workspace_mode: WorkspaceMode,
        independence_key: str,
        triggers: tuple[ExpansionTrigger, ...],
        evidence_hints: tuple[str, ...] = (),
        required: bool = False,
    ) -> Self:
        normalized = {
            "source": source.model_dump(mode="json"),
            "purpose": _normalized(purpose).casefold(),
            "work_order": _normalized(work_order).casefold(),
            "completion_check": _normalized(completion_check).casefold(),
            "workspace_mode": workspace_mode,
            "independence_key": _normalized(independence_key).casefold(),
        }
        fingerprint = stable_expansion_hash("semantic", normalized)
        return cls(
            opportunity_id=stable_expansion_hash("opportunity", loop_id, round_id, fingerprint),
            loop_id=loop_id,
            round_id=round_id,
            source=source,
            purpose=purpose,
            work_order=work_order,
            completion_check=completion_check,
            workspace_mode=workspace_mode,
            independence_key=independence_key,
            semantic_fingerprint=fingerprint,
            triggers=triggers,
            evidence_hints=evidence_hints,
            required=required,
        )


class ExpansionBlocker(_ExpansionModel):
    code: ExpansionBlockerCode
    summary: str = Field(min_length=1, max_length=2000)
    opportunity_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    retryable: bool = False


class ExpansionAssessment(_ExpansionModel):
    loop_id: str
    round_id: str
    frontier_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=64)
    level: ExpansionLevel
    opportunities: tuple[ExpansionOpportunity, ...] = ()
    blockers: tuple[ExpansionBlocker, ...] = ()
    decision_deadline_round: int | None = Field(default=None, ge=1)

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
    plan: CreateLanePlan
    definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler_version: str = Field(min_length=1, max_length=64)


class ExpansionOutcome(_ExpansionModel):
    expansion_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: ExpansionLifecycleState
    reason_code: ExpansionBlockerCode | None = None
    lane_id: str | None = None
    context_id: str | None = None
    context_revision_id: str | None = None
    directive_id: str | None = None
    run_id: str | None = None


class CuratorExpansionProposal(_ExpansionModel):
    source_context_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=1000)
    work_order: str = Field(min_length=1, max_length=8000)
    completion_check: str = Field(min_length=1, max_length=4000)
    workspace_mode: WorkspaceMode = "read_only"
    independence_key: str = Field(min_length=1, max_length=240)
    evidence_hints: tuple[str, ...] = ()
    required: bool = False
