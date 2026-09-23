r"""本文件对外提供 ExpansionAdmissionPolicy。

输入为冻结 observation、携带 WorkContextSpec 的 ExpansionOpportunity 集合和已持久化 work-spec identities；输出为版本化
ExpansionAssessment。具体工作流为逐候选检查可判定性、多 manifest 来源、授权、预算、重复与工作区安全，保留可执行候选并为每个拒绝生成稳定 blocker，
最后确定 required、recommended 或 not_applicable。示例：`assessment = policy.evaluate(observation, opportunities)`。
"""

from __future__ import annotations

from collections.abc import Iterable

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    ExpansionAssessment,
    ExpansionBlocker,
    ExpansionOpportunity,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope


class ExpansionAdmissionPolicy:
    VERSION = "semantic-expansion-admission-v2"

    def evaluate(
        self,
        observation: LoopObservationEnvelope,
        opportunities: Iterable[ExpansionOpportunity],
        *,
        existing_independence_keys: frozenset[str] = frozenset(),
    ) -> ExpansionAssessment:
        candidates = tuple(opportunities)
        if not candidates:
            return self._assessment(
                observation,
                "not_applicable",
                (),
                (ExpansionBlocker(code="not_independent", summary="当前 observation 没有可独立完成并验证的工作"),),
            )
        accepted: list[ExpansionOpportunity] = []
        blockers: list[ExpansionBlocker] = []
        for opportunity in candidates:
            blocker = self._blocker(observation, opportunity, existing_independence_keys)
            if blocker is None:
                accepted.append(opportunity)
            else:
                blockers.append(blocker)
        if not accepted:
            return self._assessment(observation, "not_applicable", candidates, tuple(blockers))
        level = "required" if any(item.required for item in accepted) else "recommended"
        rounds = int(observation.budget.get("usage", {}).get("rounds", 0) or 0)
        return self._assessment(
            observation,
            level,
            tuple(accepted),
            tuple(blockers),
            decision_deadline_round=rounds + 2 if level == "required" else None,
        )

    def _blocker(
        self,
        observation: LoopObservationEnvelope,
        opportunity: ExpansionOpportunity,
        existing_independence_keys: frozenset[str],
    ) -> ExpansionBlocker | None:
        grant = observation.grant or {}
        capabilities = set(grant.get("capabilities") or ())
        permissions = set(grant.get("permission_scope") or ())
        limits = observation.budget.get("limits", {})
        usage = observation.budget.get("usage", {})
        source_ids = {str(item.get("context_id")) for item in observation.portfolio_frontier}
        exact_sources = {
            (
                str(item.get("context_id")),
                str(item.get("revision_id")),
                str(item.get("checkpoint_id") or (item.get("revision") or {}).get("checkpoint_id") or ""),
            )
            for item in observation.portfolio_frontier
        }
        scope = set(grant.get("context_scope") or source_ids)
        source_identities = {
            (source.context_id, source.revision_id, source.checkpoint_id or "")
            for source in opportunity.manifest_sources
        }
        manifest_context_ids = {source.context_id for source in opportunity.manifest_sources}
        duplicate_keys = {item.casefold() for item in existing_independence_keys}
        isolation_available = bool(observation.workspace.get("isolation_available", True))
        workspace_mode = opportunity.work_spec.workspace_requirement
        checks = (
            ("create_lane" not in capabilities and "spawn_context" not in capabilities, "authority_missing", "当前 delegation 未授权创建派生 Context", False),
            (not opportunity.work_spec.separation_reason, "not_independent", "工作规格未声明独立认知边界", False),
            (not opportunity.work_spec.completion_criteria, "completion_not_decidable", "工作规格没有可判定完成条件", False),
            (bool(manifest_context_ids - source_ids), "source_unreadable", "manifest 来源不在冻结 Portfolio frontier", False),
            (bool(source_identities - exact_sources), "stale_source", "manifest Revision 已不再是冻结 frontier 的精确来源", False),
            (bool(manifest_context_ids - scope), "source_out_of_scope", "manifest 来源不在 delegation context scope", False),
            (int(usage.get("contexts", len(source_ids)) or 0) >= int(limits.get("max_contexts", 16) or 16), "context_budget_exhausted", "Context 预算已耗尽", True),
            (int(usage.get("lanes", len(source_ids)) or 0) >= int(limits.get("max_lanes", 8) or 8), "lane_budget_exhausted", "Lane 预算已耗尽", True),
            (int(limits.get("max_new_lanes_per_round", 3) or 0) < 1, "round_lane_budget_exhausted", "本轮不允许新增 Lane", True),
            (self._active_runs(observation) >= int(limits.get("max_concurrent_runs", 4) or 4), "concurrency_budget_exhausted", "并行 Run 预算已耗尽", True),
            (opportunity.independence_key.casefold() in duplicate_keys, "duplicate_expansion", "等价 expansion 已存在", False),
            (workspace_mode == "isolated_write" and "write" not in permissions, "workspace_conflict", "派生写入未获得 write permission", False),
            (workspace_mode == "isolated_write" and "adopt_workspace_result" not in capabilities, "workspace_isolation_unavailable", "delegation 未授权隔离写入与结果采用", True),
            (workspace_mode == "isolated_write" and not isolation_available, "workspace_isolation_unavailable", "当前 workspace 无法分配隔离 Worktree", True),
        )
        for failed, code, summary, retryable in checks:
            if failed:
                return ExpansionBlocker(
                    code=code,
                    summary=summary,
                    opportunity_id=opportunity.opportunity_id,
                    retryable=retryable,
                )
        return None

    @staticmethod
    def _active_runs(observation: LoopObservationEnvelope) -> int:
        return sum(str(item.get("status") or "").casefold() in {"pending", "queued", "running"} for item in observation.stable_results)

    def _assessment(
        self,
        observation: LoopObservationEnvelope,
        level: str,
        opportunities: tuple[ExpansionOpportunity, ...],
        blockers: tuple[ExpansionBlocker, ...],
        *,
        decision_deadline_round: int | None = None,
    ) -> ExpansionAssessment:
        return ExpansionAssessment(
            loop_id=observation.loop_id,
            round_id=observation.round_id,
            frontier_hash=observation.observed_frontier_hash,
            policy_version=self.VERSION,
            level=level,
            opportunities=opportunities,
            blockers=blockers,
            decision_deadline_round=decision_deadline_round,
        )
