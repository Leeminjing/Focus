r"""本文件对外提供 ExpansionResourcePolicy 与 resolve_expansion_resources。

输入为 Loop 授权预算中的 Context Expansion 配置、配置来源及授权修订；输出为严格校验、可序列化的版本化策略与冻结快照。
具体工作流为先验证每个容量和跨字段关系，再将默认或显式配置同授权修订绑定，供 Worker、编译器和只读投影共同消费。
示例：`frozen = resolve_expansion_resources(grant.budgets, grant.revision)`。
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.desktop.agent_loop.resource_limits import exceeds_limit, remaining_capacity


class ExpansionResourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["expansion-resource-policy-v1"] = "expansion-resource-policy-v1"
    # None means no additional ceiling; output reserve follows the model.
    max_queries: int | None = Field(default=None, ge=0)
    max_unique_candidates: int | None = Field(default=None, ge=0)
    max_exact_reads: int | None = Field(default=None, ge=0)
    max_planner_model_calls: int | None = Field(default=None, ge=0)
    max_model_attempts_per_operation: int | None = Field(default=None, ge=1)
    max_planner_tokens: int | None = Field(default=None, ge=0)
    max_compiled_evidence_items: int | None = Field(default=None, ge=0)
    max_catalog_descriptor_chars: int | None = Field(default=None, ge=1)
    max_request_input_tokens: int | None = Field(default=None, ge=1024)
    output_token_reserve: int | None = Field(default=None, ge=512)

    @model_validator(mode="after")
    def validate_related_capacity(self) -> Self:
        if self.max_exact_reads is not None and exceeds_limit(self.max_exact_reads, self.max_unique_candidates):
            raise ValueError("exact reads 不能超过唯一候选容量")
        return self


class FrozenExpansionResources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: ExpansionResourcePolicy
    source: Literal["default", "explicit", "legacy_default"]
    grant_revision: int = Field(gt=0)
    global_model_calls_remaining: int | None = Field(ge=0)
    global_input_tokens_remaining: int | None = Field(ge=0)
    global_output_tokens_remaining: int | None = Field(ge=0)


def resolve_expansion_resources(
    budgets: dict[str, Any],
    grant_revision: int,
    usage: dict[str, Any] | None = None,
) -> FrozenExpansionResources:
    usage = usage or {}
    raw_policy = budgets.get("expansion_resources")
    policy = ExpansionResourcePolicy.model_validate(raw_policy or {})
    source = budgets.get("expansion_resources_source")
    if source not in {"default", "explicit"}:
        source = "legacy_default" if raw_policy is None else "explicit"
    return FrozenExpansionResources(
        policy=policy,
        source=source,
        grant_revision=grant_revision,
        global_model_calls_remaining=remaining_capacity(budgets.get("max_model_calls", 200), int(usage.get("model_calls", 0))),
        global_input_tokens_remaining=remaining_capacity(budgets.get("max_input_tokens", 2_000_000), int(usage.get("input_tokens", 0))),
        global_output_tokens_remaining=remaining_capacity(budgets.get("max_output_tokens", 500_000), int(usage.get("output_tokens", 0))),
    )
