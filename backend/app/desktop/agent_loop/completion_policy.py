r"""本文件对外提供 CompletionCheckPolicy。

输入为当前 Mission revision 的完成检查定义与 Verifier 返回的 CriterionVerification；输出为通过或确定性
ValueError。具体工作流为要求返回集合与声明 check_id 完全一致，再逐项限制证据 kind 为该检查允许的类型，
从而阻止 outcome、boundary 或临时文字生成隐式检查。示例：`policy.validate(checks, criteria)`。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from backend.app.desktop.agent_loop.schemas import CriterionVerification


class CompletionCheckPolicy:
    def validate(
        self,
        checks: Iterable[Mapping[str, Any]],
        criteria: Iterable[CriterionVerification],
    ) -> None:
        declared = {str(item.get("check_id")): item for item in checks}
        returned = {item.check_id: item for item in criteria}
        if not declared or set(returned) != set(declared):
            raise ValueError("Completion Verifier 必须且只能评估当前 Mission 声明的 check_id")
        for check_id, criterion in returned.items():
            definition = declared[check_id]
            allowed = set(definition.get("expected_evidence_kinds") or ())
            if definition.get("user_verification"):
                allowed.add("user")
            unexpected = {item.kind for item in criterion.evidence} - allowed
            if unexpected:
                raise ValueError(f"完成检查 {check_id} 包含未声明的证据类型: {','.join(sorted(unexpected))}")
