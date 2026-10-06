r"""本文件对外提供 MissionInstructionRenderer 与 MissionBootstrapStage 的只读交付评估。

输入为冻结的有效 Mission、Loop/round、grant、Primary Context 和持久 directive/Run 事实；输出为有界普通
HumanMessage 正文、待提交的 continuation/类型化等待意图，或可显示的具体交付阻断。具体工作流为 renderer
按原文和稳定分区标识构造指令，stage 优先识别同 revision 已授权交付，再检查活动 Run、人类 gate、授权和
Context revision；已授权等待的具体 blocker 形成带证据身份的 Kernel 意图，其他情况保持明确阻断。
complete_patrol_delivery 仅向 Patrol 已选择的 Primary continuation 补入完整正文，权威 identity 与其他动作保持该次 Patrol 来源。
MissionBootstrapAssessment.to_payload 对外提供快照与规范事件共用的交付读面，不输出候选intent或正文。
示例：`assessment = await MissionBootstrapStage().assess(session, loop, round_row)`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_projection import EffectiveMission, EffectiveMissionProjector
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopDelegationGrant,
    LoopDirective,
    LoopGoalRevision,
    LoopPendingDecision,
    LoopRound,
)
from backend.app.desktop.agent_loop.schemas import PatrolDecisionIntent, WaitForUserAction
from backend.app.desktop.models import DesktopRun, DesktopThread


@dataclass(frozen=True, slots=True)
class MissionBootstrapAssessment:
    state: Literal["pending", "authorized", "delivered", "blocked", "not_required"]
    mission_revision: int
    reason: str | None = None
    directive_id: str | None = None
    run_id: str | None = None
    intent: PatrolDecisionIntent | None = None

    def to_payload(self) -> dict:
        return {"state": self.state, "mission_revision": self.mission_revision, "reason": self.reason,
                "directive_id": self.directive_id, "run_id": self.run_id}


class MissionInstructionRenderer:
    MAX_CHARS = 96000

    @classmethod
    def render(cls, mission: EffectiveMission) -> str:
        lines = ["请执行当前用户授权的任务。", "最终结果：", mission.outcome, "执行边界："]
        for label, key in (
            ("允许范围", "in_scope"),
            ("必须保持", "required_invariants"),
            ("禁止动作", "prohibited_actions"),
        ):
            values = mission.boundaries.get(key) or ()
            if values:
                lines.append(f"{label}：")
                lines.extend(f"- {value}" for value in values)
        legacy_text = mission.boundaries.get("legacy_text")
        if legacy_text:
            lines.extend(("原始执行约束：", str(legacy_text)))
        lines.append("完成检查：")
        for check in mission.completion_checks:
            lines.append(f"- [{check['check_id']}] {check['claim']}")
            if check.get("expected_evidence_kinds"):
                lines.append("  所需证据类型：" + "、".join(check["expected_evidence_kinds"]))
            if check.get("user_verification"):
                lines.append("  需要用户验证")
        lines.append("在授权范围内推进并核验；不要把任务要求当成已完成的证据。")
        content = "\n".join(lines)
        if len(content) > cls.MAX_CHARS:
            raise ValueError("mission_instruction_exceeds_window")
        return content


class MissionBootstrapStage:
    def __init__(self, renderer: type[MissionInstructionRenderer] = MissionInstructionRenderer) -> None:
        self._renderer = renderer

    @staticmethod
    def complete_patrol_delivery(intent: PatrolDecisionIntent, assessment: MissionBootstrapAssessment) -> PatrolDecisionIntent:
        seed = assessment.intent
        if seed is None:
            return intent
        primary = next((action for action in seed.actions if action.action == "continue_context"), None)
        if primary is None:
            return intent
        actions = tuple(
            action.model_copy(update={"message": primary.message + "\n\n本轮执行指令：\n" + action.message})
            if action.action == "continue_context" and action.context_id == primary.context_id else action
            for action in intent.actions
        )
        return intent.model_copy(update={"actions": actions})

    async def assess(self, session: AsyncSession, loop: AgentLoop, round_row: LoopRound) -> MissionBootstrapAssessment:
        revision = loop.goal_revision
        existing = await session.scalar(
            select(LoopDirective)
            .where(LoopDirective.loop_id == loop.loop_id, LoopDirective.goal_revision == revision)
            .order_by(LoopDirective.created_at)
            .limit(1)
        )
        if existing is not None and existing.lifecycle_state not in {"rejected", "cancelled"}:
            state = "delivered" if existing.launched_run_id else "blocked" if existing.status == "blocked" else "authorized"
            return MissionBootstrapAssessment(state, revision, existing.terminal_reason or existing.queued_reason, existing.directive_id, existing.launched_run_id)
        if round_row.number > 2 and revision == 1:
            run_count = int(await session.scalar(select(func.count()).select_from(DesktopRun).where(DesktopRun.loop_id == loop.loop_id)) or 0)
            if run_count > 1:
                return MissionBootstrapAssessment("not_required", revision, "legacy_loop_with_task_runs")
        mission = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == loop.loop_id, LoopMissionRevision.revision == revision))
        legacy = await session.scalar(select(LoopGoalRevision).where(LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == revision)) if mission is None else None
        if mission is None and legacy is None:
            return MissionBootstrapAssessment("blocked", revision, "mission_revision_missing")
        if loop.status != "running" or round_row.goal_revision != revision:
            return MissionBootstrapAssessment("blocked", revision, loop.waiting_reason or "mission_or_loop_not_current")
        view = EffectiveMissionProjector.from_rows(structured=mission, legacy=legacy)
        active_run = await session.scalar(select(DesktopRun.run_id).where(DesktopRun.loop_id == loop.loop_id, DesktopRun.status.in_(("pending", "running"))).limit(1))
        if active_run is not None:
            return MissionBootstrapAssessment("pending", revision, "active_run")
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == loop.authority_revision))
        human_gate = await session.scalar(select(LoopPendingDecision.pending_decision_id).where(LoopPendingDecision.loop_id == loop.loop_id, LoopPendingDecision.status == "pending", LoopPendingDecision.delegable.is_(False)).limit(1))
        if human_gate is not None:
            return self._wait_assessment(loop, round_row, grant, view, f"human_gate:{human_gate}", "human_gate", "gate", human_gate, f"请先处理人工 gate {human_gate}")
        blocker = self._grant_blocker(grant, loop)
        if blocker:
            permission = self.permission_identity(blocker)
            if permission is not None:
                return self._wait_assessment(loop, round_row, grant, view, blocker, "permission", "capability", permission, f"请授权 Primary Context 的 {permission} 后继续当前 Mission")
            return MissionBootstrapAssessment("blocked", revision, blocker)
        context = await session.get(DesktopThread, loop.initial_context_id)
        if context is None or context.current_revision_id is None:
            return MissionBootstrapAssessment("blocked", revision, "primary_context_unavailable")
        if context.task_id not in set(grant.context_scope):
            blocker = "primary_context_out_of_scope"
            return self._wait_assessment(loop, round_row, grant, view, blocker, "permission", "capability", self.permission_identity(blocker), "请将 Primary Context 纳入当前授权范围后继续 Mission")
        try:
            content = self._renderer.render(view)
        except ValueError as exc:
            return MissionBootstrapAssessment("blocked", revision, str(exc))
        key = f"mission-bootstrap:{loop.loop_id}:{revision}"
        intent = PatrolDecisionIntent(
            decision_id=uuid.uuid5(uuid.NAMESPACE_URL, f"focus:decision:{key}").hex,
            idempotency_key=key,
            origin_kind="mission_bootstrap",
            loop_id=loop.loop_id,
            loop_revision=loop.revision,
            round_id=round_row.round_id,
            holder_id=loop.holder_id,
            grant_id=grant.grant_id,
            grant_revision=grant.revision,
            goal_revision=revision,
            observed_frontier_hash=round_row.frontier_hash,
            observed_workspace_revision=round_row.workspace_revision,
            rationale="当前 Mission revision 尚未交付 Primary Context",
            evidence=({"kind": "mission_reference", "role": "outcome", "reference_id": "outcome", "content_hash": view.section_hashes["outcome"]},),
            actions=({"action": "continue_context", "context_id": context.task_id, "context_revision_id": context.current_revision_id, "message": content},),
        )
        return MissionBootstrapAssessment("pending", revision, intent=intent)

    def _wait_assessment(
        self,
        loop: AgentLoop,
        round_row: LoopRound,
        grant: LoopDelegationGrant | None,
        mission: EffectiveMission,
        blocker: str,
        cause: str,
        evidence_kind: str,
        evidence_id: str,
        required_input: str,
    ) -> MissionBootstrapAssessment:
        if self._grant_blocker_for_wait(grant, loop) is not None:
            return MissionBootstrapAssessment("blocked", loop.goal_revision, blocker)
        identity = f"{loop.loop_id}:{loop.goal_revision}:{grant.revision}:{round_row.round_id}:{cause}:{evidence_id}"
        token = uuid.uuid5(uuid.NAMESPACE_URL, f"focus:mission-bootstrap-wait:{identity}").hex
        intent = PatrolDecisionIntent(
            decision_id=token,
            idempotency_key=f"mission-bootstrap-wait:{token}",
            origin_kind="mission_bootstrap",
            loop_id=loop.loop_id,
            loop_revision=loop.revision,
            round_id=round_row.round_id,
            holder_id=loop.holder_id,
            grant_id=grant.grant_id,
            grant_revision=grant.revision,
            goal_revision=loop.goal_revision,
            observed_frontier_hash=round_row.frontier_hash,
            observed_workspace_revision=round_row.workspace_revision,
            rationale=f"当前 Mission 交付受阻：{blocker}",
            evidence=({"kind": "mission_reference", "role": "outcome", "reference_id": "outcome", "content_hash": mission.section_hashes["outcome"]},),
            actions=(WaitForUserAction(
                action="wait_for_user",
                reason=required_input,
                cause=cause,
                required_input=required_input,
                evidence_identity={"kind": evidence_kind, "reference_id": evidence_id, "revision": loop.goal_revision},
            ),),
        )
        return MissionBootstrapAssessment("blocked", loop.goal_revision, blocker, intent=intent)

    @staticmethod
    def permission_identity(blocker: str) -> str | None:
        return {
            "continue_context_not_authorized": "continue_context",
            "primary_context_out_of_scope": "primary_context_scope",
        }.get(blocker)

    @staticmethod
    def _grant_blocker_for_wait(grant: LoopDelegationGrant | None, loop: AgentLoop) -> str | None:
        if grant is None or grant.status != "active" or grant.holder_id != loop.holder_id:
            return "delegation_grant_missing_or_revoked"
        if grant.expires_at is not None and grant.expires_at <= datetime.now(UTC):
            return "delegation_grant_expired"
        if "wait_for_user" not in set(grant.capabilities or ()):
            return "wait_for_user_not_authorized"
        return None

    @staticmethod
    def _grant_blocker(grant: LoopDelegationGrant | None, loop: AgentLoop) -> str | None:
        if grant is None or grant.status != "active" or grant.holder_id != loop.holder_id:
            return "delegation_grant_missing_or_revoked"
        if grant.expires_at is not None and grant.expires_at <= datetime.now(UTC):
            return "delegation_grant_expired"
        if "continue_context" not in set(grant.capabilities):
            return "continue_context_not_authorized"
        return None
