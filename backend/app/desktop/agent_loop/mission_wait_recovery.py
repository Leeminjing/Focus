r"""本文件对外提供 MissionWaitRecoveryGuard 与 MissionWaitRecoveryRejected。

输入为用户确认的类型化缺失目标请求、已锁定 Loop 及当前 Mission/grant/round/Context/预算事实；输出为可安全继续的
当前 LoopRound，或具体拒绝原因。具体工作流为拒绝无法证明语义的旧版文本澄清及真实缺失输入，验证当前 Mission revision、有效授权、
无活动 Run/人工 gate、活跃 Primary frontier 和硬预算，再交由调用方原子 supersede 请求并创建继任轮。
示例：`current = await guard.validate(session, loop, request, request_revision=1)`。
缺省 outcome 按未提供处理，不授予旧 missing_goal 的自动恢复资格。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.budgets import LoopBudgetGuard, configured_provider_count
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from backend.app.desktop.agent_loop.models import (
    AgentLoop, LoopBudgetUsage, LoopContextMembership, LoopDelegationGrant, LoopGoalRevision,
    LoopPendingDecision, LoopRound,
)
from backend.app.desktop.agent_loop.rounds import current_frontier_hash
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest
from backend.app.desktop.models import DesktopRun, DesktopThread


class MissionWaitRecoveryRejected(ValueError):
    pass


class MissionWaitRecoveryGuard:
    async def validate(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        request: LoopWaitRequest,
        *,
        request_revision: int,
    ) -> LoopRound:
        scope = request.scope or {}
        evidence = scope.get("evidence_identity") or {}
        if (
            request.kind != "clarification"
            or request.response_mode != "text"
            or scope.get("cause") != "missing_goal"
            or not isinstance(evidence, dict)
            or evidence.get("kind") != "mission"
            or evidence.get("reference_id") != "outcome"
        ):
            raise MissionWaitRecoveryRejected("只有证据身份明确的缺失目标澄清可沿用当前 Mission；旧版无类型澄清及输入、授权和预算等待必须分别解决")
        if request.status != "open" or request.revision != request_revision or loop.status != "waiting_user":
            raise MissionWaitRecoveryRejected("等待请求状态或版本已变化")
        if scope.get("mission_revision") not in {None, loop.goal_revision}:
            raise MissionWaitRecoveryRejected("等待请求对应的 Mission revision 已变化")
        if evidence.get("revision") not in {None, loop.goal_revision}:
            raise MissionWaitRecoveryRejected("等待请求的 Mission 证据 revision 已变化")
        await self._require_mission(session, loop)
        grant = await self._require_grant(session, loop)
        current = await self._require_round_and_frontier(session, loop, request)
        await self._require_no_gate_or_run(session, loop)
        await self._require_budget(session, loop, grant)
        return current

    @staticmethod
    async def _require_mission(session: AsyncSession, loop: AgentLoop) -> None:
        structured = await session.scalar(select(LoopMissionRevision).where(
            LoopMissionRevision.loop_id == loop.loop_id, LoopMissionRevision.revision == loop.goal_revision,
        ))
        legacy = await session.scalar(select(LoopGoalRevision).where(
            LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == loop.goal_revision,
        )) if structured is None else None
        if structured is None and legacy is None:
            raise MissionWaitRecoveryRejected("当前 Mission revision 不存在")
        mission = EffectiveMissionProjector.from_rows(structured=structured, legacy=legacy)
        if not (mission.outcome or "").strip() or not mission.completion_checks:
            raise MissionWaitRecoveryRejected("当前 Mission 缺少最终结果或完成检查")

    @staticmethod
    async def _require_grant(session: AsyncSession, loop: AgentLoop) -> LoopDelegationGrant:
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id,
            LoopDelegationGrant.revision == loop.authority_revision,
            LoopDelegationGrant.status == "active",
        ))
        if grant is None or grant.holder_id != loop.holder_id or (
            grant.expires_at is not None and grant.expires_at <= datetime.now(UTC)
        ) or "continue_context" not in set(grant.capabilities or ()) or loop.initial_context_id not in set(grant.context_scope or ()):
            raise MissionWaitRecoveryRejected("当前授权不允许继续 Primary Context")
        return grant

    @staticmethod
    async def _require_round_and_frontier(session: AsyncSession, loop: AgentLoop, request: LoopWaitRequest) -> LoopRound:
        current = await session.get(LoopRound, loop.current_round_id, with_for_update=True) if loop.current_round_id else None
        if current is None or current.status not in {"settled", "error"} or current.goal_revision != loop.goal_revision or current.authority_revision != loop.authority_revision:
            raise MissionWaitRecoveryRejected("当前 round 或 Mission/授权版本无法安全恢复")
        if request.round_id is not None and request.round_id != current.round_id:
            raise MissionWaitRecoveryRejected("等待请求不属于当前 round")
        primary = await session.scalar(select(LoopContextMembership.membership_id).where(
            LoopContextMembership.loop_id == loop.loop_id,
            LoopContextMembership.context_id == loop.initial_context_id,
            LoopContextMembership.status == "active",
        ).limit(1))
        context = await session.get(DesktopThread, loop.initial_context_id)
        if primary is None or context is None or context.current_revision_id is None or await current_frontier_hash(session, loop.loop_id) is None:
            raise MissionWaitRecoveryRejected("Primary Context frontier 当前不可运行")
        return current

    @staticmethod
    async def _require_no_gate_or_run(session: AsyncSession, loop: AgentLoop) -> None:
        gate = await session.scalar(select(LoopPendingDecision.pending_decision_id).where(
            LoopPendingDecision.loop_id == loop.loop_id, LoopPendingDecision.status == "pending",
            LoopPendingDecision.delegable.is_(False),
        ).limit(1))
        active_run = await session.scalar(select(DesktopRun.run_id).where(
            DesktopRun.loop_id == loop.loop_id, DesktopRun.status.in_(("pending", "running")),
        ).limit(1))
        if gate is not None or active_run is not None:
            raise MissionWaitRecoveryRejected("仍有人工 gate 或活动 Run，不能恢复")

    @staticmethod
    async def _require_budget(session: AsyncSession, loop: AgentLoop, grant: LoopDelegationGrant) -> None:
        usage = await session.get(LoopBudgetUsage, loop.loop_id)
        values = {} if usage is None else {name: int(getattr(usage, name, 0) or 0) for name in LoopBudgetGuard.HARD_FIELDS}
        created_at = loop.created_at if loop.created_at.tzinfo else loop.created_at.replace(tzinfo=UTC)
        values["duration_seconds"] = max(0, int((datetime.now(UTC) - created_at).total_seconds()))
        values["providers"] = configured_provider_count(loop.equipment or {})
        values["contexts"] = int(await session.scalar(select(func.count()).select_from(LoopContextMembership).where(
            LoopContextMembership.loop_id == loop.loop_id, LoopContextMembership.status != "discarded",
        )) or 0)
        budget = LoopBudgetGuard().evaluate(values, grant.budgets or {}, "continue_context", {"rounds": 1})
        if budget.status != "allow":
            raise MissionWaitRecoveryRejected("当前预算或无进展限制不允许继续：" + ",".join(budget.reasons))
