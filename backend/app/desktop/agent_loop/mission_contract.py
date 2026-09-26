r"""本文件对外提供 ExecutionBoundaries、CompletionCheckDefinition、LoopMissionContract 与 LegacyMissionAdapter。

输入为用户编写的最终结果、边界分组、完成检查或旧 goal/task_contract/acceptance_criteria；输出为不可变、
职责互斥且可序列化的 Mission contract。具体工作流为先规范化文本和证据类别，再验证跨分区重复、检查标识
与证据要求；禁止动作中的规范 action name 和范围中的 `context:<id>` 可供 Kernel 确定性执行，旧数据则
保留完整 contract 文本并生成稳定检查标识。示例：`LegacyMissionAdapter.convert(...)`。
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


EvidenceKind = Literal["test", "tool", "artifact", "workspace", "fact", "user"]
MACHINE_ACTION_TYPES = frozenset({
    "continue_context",
    "create_lane",
    "update_lane",
    "merge_contexts",
    "recover_context",
    "pause_lane",
    "discard_membership",
    "request_lane_curator",
    "request_completion_verifier",
    "request_completion",
    "adopt_workspace_result",
    "apply_context_compression",
    "wait_for_user",
    "stop_loop",
})


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalized_items(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _normalized_text(value)
        key = item.casefold()
        if item and key not in seen:
            result.append(item)
            seen.add(key)
    return tuple(result)


class _MissionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionBoundaries(_MissionModel):
    in_scope: tuple[str, ...] = ()
    required_invariants: tuple[str, ...] = ()
    prohibited_actions: tuple[str, ...] = ()
    legacy_text: str | None = None

    @field_validator("in_scope", "required_invariants", "prohibited_actions", mode="before")
    @classmethod
    def normalize_groups(cls, value: Any) -> tuple[str, ...]:
        return _normalized_items(value)

    @field_validator("legacy_text", mode="before")
    @classmethod
    def preserve_legacy_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        return text if text else None

    def statements(self) -> tuple[str, ...]:
        return (*self.in_scope, *self.required_invariants, *self.prohibited_actions)

    def blocked_action_types(self) -> tuple[str, ...]:
        return tuple(item for item in self.prohibited_actions if item in MACHINE_ACTION_TYPES)

    def scoped_context_ids(self) -> tuple[str, ...]:
        return tuple(item.removeprefix("context:") for item in self.in_scope if item.startswith("context:") and item != "context:")


class CompletionCheckDefinition(_MissionModel):
    check_id: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    claim: str = Field(min_length=1, max_length=4000)
    required: bool = True
    expected_evidence_kinds: tuple[EvidenceKind, ...] = ()
    user_verification: bool = False

    @field_validator("claim", mode="before")
    @classmethod
    def normalize_claim(cls, value: Any) -> str:
        return _normalized_text(value)

    @field_validator("expected_evidence_kinds", mode="before")
    @classmethod
    def normalize_evidence(cls, value: Any) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value or ()))

    @model_validator(mode="after")
    def require_evidence_contract(self) -> "CompletionCheckDefinition":
        if not self.expected_evidence_kinds and not self.user_verification:
            raise ValueError("完成检查必须声明证据类别或人工验证")
        return self


class LoopMissionContract(_MissionModel):
    outcome: str = Field(min_length=1, max_length=12000)
    boundaries: ExecutionBoundaries = Field(default_factory=ExecutionBoundaries)
    completion_checks: tuple[CompletionCheckDefinition, ...] = Field(min_length=1)

    @field_validator("outcome", mode="before")
    @classmethod
    def normalize_outcome(cls, value: Any) -> str:
        return _normalized_text(value)

    @model_validator(mode="after")
    def separate_semantic_roles(self) -> "LoopMissionContract":
        entries = [
            ("最终结果", self.outcome),
            *(("执行边界", item) for item in self.boundaries.statements()),
            *(("完成检查", item.claim) for item in self.completion_checks),
        ]
        locations: dict[str, str] = {}
        for location, value in entries:
            key = _normalized_text(value).casefold()
            previous = locations.get(key)
            if previous is not None and previous != location:
                raise ValueError(f"相同内容不能同时属于{previous}和{location}")
            locations[key] = location
        check_ids = [item.check_id for item in self.completion_checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("完成检查 check_id 必须唯一")
        return self


class LegacyMissionAdapter:
    @classmethod
    def convert(
        cls,
        *,
        goal: str,
        task_contract: str,
        acceptance_criteria: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ) -> LoopMissionContract:
        checks = tuple(cls._check(item, index) for index, item in enumerate(acceptance_criteria))
        return LoopMissionContract(
            outcome=goal,
            boundaries=ExecutionBoundaries(legacy_text=task_contract),
            completion_checks=checks,
        )

    @staticmethod
    def export(contract: LoopMissionContract) -> dict[str, Any]:
        boundary_lines = []
        for title, statements in (
            ("范围", contract.boundaries.in_scope),
            ("必须保持", contract.boundaries.required_invariants),
            ("禁止", contract.boundaries.prohibited_actions),
        ):
            if statements:
                boundary_lines.append(f"{title}：")
                boundary_lines.extend(f"- {item}" for item in statements)
        task_contract = (
            contract.boundaries.legacy_text
            if contract.boundaries.legacy_text is not None
            else "\n".join(boundary_lines)
        )
        return {
            "goal": contract.outcome,
            "task_contract": task_contract,
            "acceptance_criteria": [
                {
                    "criterion_id": item.check_id,
                    "text": item.claim,
                    "required": item.required,
                    "expected_evidence_kinds": list(item.expected_evidence_kinds),
                    "user_verification": item.user_verification,
                }
                for item in contract.completion_checks
            ],
        }

    @staticmethod
    def _check(item: dict[str, Any], index: int) -> CompletionCheckDefinition:
        claim = _normalized_text(item.get("text") or item.get("criterion_id"))
        raw_id = _normalized_text(item.get("criterion_id"))
        check_id = raw_id or f"legacy-{sha256(f'{index}:{claim}'.encode('utf-8')).hexdigest()[:16]}"
        raw_kinds = item.get("expected_evidence_kinds") or item.get("evidence_kinds") or ("fact",)
        user_verification = bool(item.get("user_verification", False))
        return CompletionCheckDefinition(
            check_id=check_id,
            claim=claim,
            required=bool(item.get("required", True)),
            expected_evidence_kinds=tuple(raw_kinds),
            user_verification=user_verification,
        )
