r"""本文件对外提供 ContextRecoveryAuthorityValidator 与 ContextRecoveryAuthorityError。

输入为持久 recovery opportunity、当前 Loop/goal/workspace/authority/grant/frontier/source revision、可信 Lane plan 和校验时刻；输出为
通过的空结果或带稳定错误码的 authority/freshness 异常。具体工作流为先验证状态、编译器与过期时间，再比较所有权威 revision 和
单来源计划，任何不一致都在 Kernel 授权或 Portfolio 发布前失败。示例：`validator.validate(opportunity, ..., plan=plan)`。
"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.app.desktop.agent_loop.context_recovery.contracts import (
    CONTEXT_RECOVERY_COMPILER_VERSION,
    ContextRecoveryOpportunityContract,
)
from backend.app.desktop.context_curation import CreateLanePlan


class ContextRecoveryAuthorityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ContextRecoveryAuthorityValidator:
    def validate(
        self,
        opportunity: ContextRecoveryOpportunityContract,
        *,
        loop_id: str,
        goal_revision: int,
        workspace_revision: int,
        authority_revision: int,
        grant_id: str,
        grant_revision: int,
        frontier_hash: str,
        current_source_revision_id: str | None,
        plan: CreateLanePlan,
        now: datetime | None = None,
    ) -> None:
        current_time = now or datetime.now(UTC)
        expires_at = self._aware(opportunity.expires_at)
        if opportunity.status != "pending":
            self._reject("opportunity_consumed", "Context recovery opportunity 不存在或已消费")
        if opportunity.compiler_version != CONTEXT_RECOVERY_COMPILER_VERSION:
            self._reject("compiler_version_changed", "Context recovery compiler version 已变化")
        if expires_at <= self._aware(current_time):
            self._reject("opportunity_expired", "Context recovery opportunity 已过期")
        expected = opportunity.plan
        actual = self._without_identity(plan)
        checks = (
            (opportunity.loop_id == loop_id, "loop_changed"),
            (opportunity.source_frontier_hash == frontier_hash, "frontier_changed"),
            (opportunity.goal_revision == goal_revision, "goal_changed"),
            (opportunity.workspace_revision == workspace_revision, "workspace_changed"),
            (opportunity.authority_revision == authority_revision, "authority_changed"),
            (opportunity.grant_id == grant_id, "grant_changed"),
            (opportunity.grant_revision == grant_revision, "grant_revision_changed"),
            (current_source_revision_id == opportunity.source.revision_id, "source_revision_changed"),
            (plan.source_frontier == (opportunity.source,), "source_frontier_changed"),
            (actual == expected, "recovery_plan_changed"),
        )
        for valid, code in checks:
            if not valid:
                self._reject(code, f"Context recovery opportunity authority 或 freshness 已变化: {code}")

    @staticmethod
    def _without_identity(plan: CreateLanePlan) -> CreateLanePlan:
        return plan.model_copy(
            update={
                "lane_policy": {
                    key: value
                    for key, value in plan.lane_policy.items()
                    if key != "recovery_opportunity_id"
                }
            }
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _reject(code: str, message: str) -> None:
        raise ContextRecoveryAuthorityError(code, message)
