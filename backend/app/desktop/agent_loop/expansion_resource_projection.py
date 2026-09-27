r"""本文件对外提供 expansion_resource_view、planning_session_view 与 repeated_expansion_blocker。

输入为 Loop grant 预算/修订、全局用量及只读 planning session 记录；输出为有效策略、剩余容量和因果阻断的安全展示结构。
具体工作流为兼容旧 grant 默认策略，优先展示已冻结 session 的原值，再按唯一候选和真实模型用量计算剩余量；重复阻断只匹配相邻决策轮的同一来源及资源边界，不暴露模型原文。
示例：`view = expansion_resource_view(grant.budgets, grant.revision, usage, sessions)`。
"""

from __future__ import annotations

from typing import Any

from backend.app.desktop.agent_loop.expansion_resource_policy import resolve_expansion_resources


def planning_session_view(payload: dict[str, Any], *, round_id: str, round_number: int | None = None) -> dict[str, Any]:
    frozen = payload.get("frozen_resources") or {}
    policy = frozen.get("policy") or {}
    usage = payload.get("usage") or {}
    limits = payload.get("budget") or {}
    pairs = {
        "queries": ("max_queries", "queries"),
        "unique_candidates": ("max_candidates", "candidates"),
        "exact_reads": ("max_exact_reads", "exact_reads"),
        "planner_model_calls": ("max_model_calls", "model_calls"),
        "planner_tokens": ("max_tokens", "tokens"),
    }
    remaining = {
        name: max(0, int(limits.get(limit, 0)) - int(usage.get(used, 0)))
        for name, (limit, used) in pairs.items()
    }
    return {
        "session_id": payload.get("session_id"),
        "round_id": round_id,
        "round_number": round_number,
        "source_context_ids": payload.get("source_context_ids") or (),
        "state": payload.get("state"),
        "stage": payload.get("blocker_stage") or "retrieval_planning",
        "schema_version": payload.get("schema_version", "retrieval-session-v1"),
        "policy_version": policy.get("version"),
        "source": frozen.get("source", "historical"),
        "grant_revision": frozen.get("grant_revision"),
        "policy": policy,
        "limits": limits,
        "usage": usage,
        "remaining": remaining,
        "query_coverage": payload.get("query_coverage") or (),
        "blocker_code": payload.get("blocker_code"),
        "blocker_summary": payload.get("blocker_summary"),
        "blocker_boundary": payload.get("blocker_boundary"),
        "blocker_operation_id": payload.get("blocker_operation_id"),
        "required_expansion_evaluation": bool(payload.get("required_expansion_evaluation", False)),
    }


def expansion_resource_view(
    budgets: dict[str, Any],
    grant_revision: int,
    global_usage: dict[str, Any],
    sessions: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    current = resolve_expansion_resources(budgets, grant_revision, global_usage)
    return {
        "effective": current.model_dump(mode="json"),
        "latest_session": sessions[-1] if sessions else None,
        "session_count": len(sessions),
        "repeated_blocker": repeated_expansion_blocker(sessions),
    }


def repeated_expansion_blocker(sessions: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    by_round: dict[int, dict[tuple[str, ...], dict[str, Any]]] = {}
    for item in sessions:
        number = item.get("round_number")
        contexts = tuple(item.get("source_context_ids") or ())
        if number is not None and contexts:
            by_round.setdefault(int(number), {})[contexts] = item
    if len(by_round) < 2:
        return None
    rounds = sorted(by_round)
    earlier, later = rounds[-2:]
    internal_codes = {"expansion_policy_limit", "retrieval_budget_exhausted", "portfolio_catalog_overflow"}
    if later != earlier + 1:
        return None
    for contexts, second in by_round[later].items():
        boundary = second.get("blocker_boundary")
        if not second.get("required_expansion_evaluation") or second.get("blocker_code") not in internal_codes or not boundary:
            continue
        first = by_round[earlier].get(contexts)
        if first is None or not first.get("required_expansion_evaluation") or (
            first.get("blocker_code"), first.get("blocker_boundary")
        ) != (second["blocker_code"], boundary):
            continue
        return {
            "code": second["blocker_code"],
            "boundary": boundary,
            "source_context_ids": contexts,
            "consecutive_rounds": 2,
            "first_round": earlier,
            "last_round": later,
            "stage": second.get("stage"),
            "summary": second.get("blocker_summary"),
            "action": "Context Expansion 已连续受同一内部资源边界阻断；请查看策略与用量后修订授权容量。",
        }
    return None
