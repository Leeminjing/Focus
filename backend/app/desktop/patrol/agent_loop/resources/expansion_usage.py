r"""本文件对外提供 ExpansionUsageCharge 与 ExpansionUsageLedger。

输入为冻结 session/stage 的稳定 ledger identity 和 query、唯一候选、精读、模型调用或编译证据操作；输出为幂等的不可变用量账本与分类汇总。
具体工作流为按 operation identity 原子追加 charge，相同操作重放不重复计数，冲突重放拒绝；模型调用分别保留真实输入和输出 Token。
示例：`ledger = ledger.record(ExpansionUsageCharge(operation_id="query:q1", kind="query"))`。
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


UsageKind = Literal["query", "unique_candidate", "exact_read", "model_call", "compiled_evidence_item"]


class ExpansionUsageCharge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(min_length=1, max_length=256)
    kind: UsageKind
    units: int = Field(default=1, ge=1)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    usage_reported: bool = True

    @model_validator(mode="after")
    def validate_model_usage(self) -> Self:
        if self.kind != "model_call" and (self.input_tokens or self.output_tokens):
            raise ValueError("非模型调用不得携带 Token 用量")
        if self.kind != "model_call" and not self.usage_reported:
            raise ValueError("非模型调用不得标记 provider usage 缺失")
        return self


class ExpansionUsageLedger(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ledger_id: str = Field(min_length=1)
    charges: tuple[ExpansionUsageCharge, ...] = ()

    @model_validator(mode="after")
    def require_unique_operations(self) -> Self:
        if len({charge.operation_id for charge in self.charges}) != len(self.charges):
            raise ValueError("usage ledger operation identity 重复")
        return self

    def record(self, charge: ExpansionUsageCharge) -> Self:
        previous = next((item for item in self.charges if item.operation_id == charge.operation_id), None)
        if previous is not None:
            if previous != charge:
                raise ValueError("usage ledger operation identity 冲突")
            return self
        return self.model_copy(update={"charges": (*self.charges, charge)})

    def total(self, kind: UsageKind) -> int:
        return sum(item.units for item in self.charges if item.kind == kind)

    @property
    def input_tokens(self) -> int:
        return sum(item.input_tokens for item in self.charges)

    @property
    def output_tokens(self) -> int:
        return sum(item.output_tokens for item in self.charges)

    @property
    def unreported_model_calls(self) -> int:
        return sum(item.units for item in self.charges if item.kind == "model_call" and not item.usage_reported)
