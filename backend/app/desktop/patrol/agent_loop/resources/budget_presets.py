"""工作区自动执行的产品预算；首次授权与追加预算共用同一份定义。

时长沿用 Loop 从创建起的墙钟口径（包括暂停和等待），追加基于已占用量，
保留未设限字段及现有更高的并发授权；不改变权限或清零用量。
"""

from typing import Literal

from backend.app.desktop.patrol.agent_loop.schemas import LoopBudgetContract

BudgetPreset = Literal["normal", "long"]

PRESETS = {
    "normal": {"label": "普通任务", "budgets": {
        "max_rounds": 40, "max_duration_seconds": 1800, "max_model_calls": 100,
        "max_input_tokens": 500_000, "max_output_tokens": 50_000,
        "max_retries": 6, "max_concurrent_runs": 2,
    }},
    "long": {"label": "长任务", "budgets": {
        "max_rounds": 160, "max_duration_seconds": 7200, "max_model_calls": 400,
        "max_input_tokens": 2_000_000, "max_output_tokens": 200_000,
        "max_retries": 20, "max_concurrent_runs": 4,
    }},
}


def initial_budgets(preset: BudgetPreset | None = None) -> dict:
    return LoopBudgetContract.model_validate(PRESETS[preset or "normal"]["budgets"]).as_grant_budgets()


def increased_budgets(current: dict, usage: dict, preset: BudgetPreset) -> LoopBudgetContract:
    patch = {}
    for field, increment in PRESETS[preset]["budgets"].items():
        limit = current.get(field)
        if limit is None:
            continue
        if field == "max_concurrent_runs":
            patch[field] = max(limit, increment)
        else:
            patch[field] = max(limit, usage.get(field.removeprefix("max_"), 0)) + increment
    if "max_rounds" in patch:
        patch["max_rounds"] = min(1000, patch["max_rounds"])
    return LoopBudgetContract.model_validate(patch)
