r"""本文件对外提供 LoopBudgetGuard、BudgetDecision、configured_provider_count 与进展 fingerprint。

输入为 grant hard/soft budgets、持久 usage、下一动作预留量、运行装备和本轮目标/失败/workspace/action/result 摘要；输出为
allow、change_direction、exhausted 决定、稳定进展 fingerprint 或已配置模型来源数。具体工作流为先检查
全部硬上限，再比较等价失败 fingerprint，达到阈值时拒绝泛化 continue 并要求改变 Context 或等待用户。
示例：`guard.evaluate(usage, budgets, "continue_context")`。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict


class BudgetDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    reasons: tuple[str, ...]


class LoopBudgetGuard:
    HARD_FIELDS = {"rounds": "max_rounds", "duration_seconds": "max_duration_seconds", "model_calls": "max_model_calls", "input_tokens": "max_input_tokens", "output_tokens": "max_output_tokens", "retries": "max_retries", "lanes": "max_lanes", "contexts": "max_contexts", "providers": "max_providers"}

    def evaluate(self, usage: dict[str, int], budgets: dict[str, int], next_action: str, projected: dict[str, int] | None = None) -> BudgetDecision:
        projected = projected or {}
        exhausted = [
            name
            for name, limit in self.HARD_FIELDS.items()
            if int(usage.get(name, 0)) >= int(budgets.get(limit, 2**63 - 1))
            or int(usage.get(name, 0)) + int(projected.get(name, 0)) > int(budgets.get(limit, 2**63 - 1))
        ]
        if exhausted:
            return BudgetDecision(status="exhausted", reasons=tuple(f"{name}_budget" for name in exhausted))
        if next_action == "continue_context" and int(usage.get("no_progress_count", 0)) >= int(budgets.get("max_no_progress", 3)):
            return BudgetDecision(status="change_direction", reasons=("no_progress_threshold",))
        return BudgetDecision(status="allow", reasons=())


def no_progress_fingerprint(**facts: Any) -> str:
    payload = json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def configured_provider_count(equipment: dict[str, Any]) -> int:
    models = {
        str(value)
        for key, value in (equipment or {}).items()
        if (key == "model_name" or key.endswith("_model_name")) and value
    }
    return len(models)
