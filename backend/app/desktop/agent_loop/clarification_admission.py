r"""本文件对外提供 ClarificationFacts、ClarificationFactsReader 和 ClarificationAdmissionPolicy。

输入为类型化 wait_for_user 提案及冻结 observation 或 Kernel 当前 Mission/grant/gate/预算事实；输出为准入结果
或包含合法 cause/证据 identity 的 read_view。具体工作流为 Reader 在当前事务校验同 Observation 的 assessment 补充，
不存在补充时兼容基础 envelope；当前授权、预算、gate 与 Run 仍由权威事实组装。Policy 的读面与验证共享证据资格，
Kernel 另核对尚未交付 Mission 的 Primary 授权缺口。完整 Mission、安全继续及没有派生机会不能构成缺失目标等待。
示例：`view = ClarificationAdmissionPolicy.read_view(facts)`；`ClarificationAdmissionPolicy().validate(action, facts)`。
workspace 不以缺少 outcome/checks 索取补表，可由冻结用户输入证明具体缺口，活动安全工作不阻止独立信息请求。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.budgets import LoopBudgetGuard
from backend.app.desktop.agent_loop.decision_context import DecisionSupplementRepository
from backend.app.desktop.agent_loop.mission_bootstrap import MissionBootstrapStage
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDelegationGrant, LoopGoalRevision, LoopObservation, LoopPendingDecision, LoopRound
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope, WaitForUserAction
from backend.app.desktop.models import DesktopRun


class ClarificationRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ClarificationFacts:
    mission_revision: int
    outcome: str
    check_ids: frozenset[str]
    capabilities: frozenset[str]
    pending_gate_ids: frozenset[str]
    budget_exhaustions: frozenset[str]
    external_blockers: frozenset[str]
    safe_continuation: bool
    active_run: bool = False
    missing_input_ids: frozenset[str] = frozenset()
    permission_needs: frozenset[str] = frozenset()
    workspace_patrol: bool = False
    user_input_ids: frozenset[str] = frozenset()

    @classmethod
    def from_observation(cls, observation: LoopObservationEnvelope) -> ClarificationFacts:
        mission = EffectiveMissionProjector.observation_payload(observation)["effective_mission"] or {}
        grant = observation.grant or {}
        pending = frozenset(
            str(item.get("pending_decision_id"))
            for item in observation.pending_decisions
            if item.get("pending_decision_id") and item.get("status") == "pending" and not item.get("delegable")
        )
        missing = frozenset(
            str(item.get("pending_decision_id"))
            for item in observation.pending_decisions
            if item.get("pending_decision_id") and item.get("status") == "pending" and item.get("kind") == "missing_input"
        )
        budget = LoopBudgetGuard().evaluate(observation.budget.get("usage") or {}, observation.budget.get("limits") or {}, "continue_context")
        exhausted = frozenset(budget.reasons if budget.status == "exhausted" else ())
        external = frozenset({"recovery_waiting_reason"} if observation.recovery_waiting_reason else ())
        capabilities = frozenset(grant.get("capabilities") or ())
        permissions = frozenset(grant.get("permission_scope") or ())
        scope = frozenset(grant.get("context_scope") or ())
        primary = str(observation.portfolio_frontier[0].get("context_id")) if observation.portfolio_frontier else ""
        safe = "continue_context" in capabilities and primary in scope and not pending and not exhausted and not external
        return cls(
            workspace_patrol=observation.interaction_mode == "workspace_patrol",
            user_input_ids=frozenset(item["intent_id"] for item in observation.user_intents if item.get("intent_kind") == "workspace_input"),
            mission_revision=observation.goal_revision,
            outcome=str(mission.get("outcome") or ""),
            check_ids=frozenset(str(item.get("check_id")) for item in mission.get("completion_checks") or () if item.get("check_id")),
            capabilities=capabilities,
            pending_gate_ids=pending,
            budget_exhaustions=exhausted,
            external_blockers=external,
            safe_continuation=safe,
            missing_input_ids=missing,
            permission_needs=ClarificationAdmissionPolicy.permission_needs(observation.expansion_assessment, capabilities, permissions),
        )


class ClarificationFactsReader:
    async def read(self, session: AsyncSession, loop: AgentLoop, grant: LoopDelegationGrant | None, round_row: LoopRound) -> ClarificationFacts:
        mission = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == loop.loop_id, LoopMissionRevision.revision == loop.goal_revision))
        legacy = await session.scalar(select(LoopGoalRevision).where(LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == loop.goal_revision)) if mission is None else None
        view = EffectiveMissionProjector.from_rows(structured=mission, legacy=legacy)
        gates = frozenset((await session.scalars(select(LoopPendingDecision.pending_decision_id).where(LoopPendingDecision.loop_id == loop.loop_id, LoopPendingDecision.status == "pending", LoopPendingDecision.delegable.is_(False)))).all())
        missing = frozenset((await session.scalars(select(LoopPendingDecision.pending_decision_id).where(LoopPendingDecision.loop_id == loop.loop_id, LoopPendingDecision.status == "pending", LoopPendingDecision.kind == "missing_input"))).all())
        active = await session.scalar(select(DesktopRun.run_id).where(DesktopRun.loop_id == loop.loop_id, DesktopRun.status.in_(("pending", "running"))).limit(1))
        usage = await session.get(LoopBudgetUsage, loop.loop_id)
        budget_values = {} if usage is None else {name: int(getattr(usage, name, 0) or 0) for name in LoopBudgetGuard.HARD_FIELDS}
        created_at = loop.created_at if loop.created_at.tzinfo else loop.created_at.replace(tzinfo=UTC)
        budget_values["duration_seconds"] = max(0, int((datetime.now(UTC) - created_at).total_seconds()))
        budget = LoopBudgetGuard().evaluate(budget_values, grant.budgets if grant is not None else {}, "continue_context")
        exhausted = frozenset(budget.reasons if budget.status == "exhausted" else ())
        observation = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == round_row.round_id))
        external = frozenset({"recovery_waiting_reason"} if observation is not None and (observation.envelope or {}).get("recovery_waiting_reason") else ())
        capabilities = frozenset(grant.capabilities or ()) if grant is not None else frozenset()
        permissions = frozenset(grant.permission_scope or ()) if grant is not None else frozenset()
        scope = frozenset(grant.context_scope or ()) if grant is not None else frozenset()
        safe = "continue_context" in capabilities and loop.initial_context_id in scope and not gates and not exhausted and not external and active is None
        bootstrap = await MissionBootstrapStage().assess(session, loop, round_row)
        bootstrap_need = MissionBootstrapStage.permission_identity(bootstrap.reason or "") if bootstrap.state == "blocked" else None
        assessment = await self._expansion_assessment(session, observation)
        permission_needs = ClarificationAdmissionPolicy.permission_needs(assessment, capabilities, permissions)
        if bootstrap_need is not None:
            permission_needs = permission_needs | {bootstrap_need}
        return ClarificationFacts(
            workspace_patrol=loop.interaction_mode == "workspace_patrol",
            user_input_ids=frozenset(item["intent_id"] for item in (observation.envelope if observation else {}).get("user_intents", ())
                if item.get("intent_kind") == "workspace_input"),
            mission_revision=loop.goal_revision,
            outcome=view.outcome or "",
            check_ids=frozenset(str(item["check_id"]) for item in view.completion_checks),
            capabilities=capabilities,
            pending_gate_ids=gates,
            budget_exhaustions=exhausted,
            external_blockers=external,
            safe_continuation=safe,
            active_run=active is not None,
            missing_input_ids=missing,
            permission_needs=frozenset(permission_needs),
        )

    @staticmethod
    async def _expansion_assessment(session: AsyncSession, observation: LoopObservation | None) -> dict | None:
        if observation is None:
            return None
        supplement = await DecisionSupplementRepository().get(session, observation.observation_id, "expansion_assessment")
        return supplement if supplement is not None else (observation.envelope or {}).get("expansion_assessment")


class ClarificationAdmissionPolicy:
    @classmethod
    def read_view(cls, facts: ClarificationFacts) -> dict:
        return {
            "version": "clarification-admission-read-v1",
            "mission_revision": facts.mission_revision,
            "safe_continuation": facts.safe_continuation,
            "active_run": facts.active_run,
            "admitted_requests": tuple(
                {"cause": cause, "evidence_identity": {"kind": kind, "reference_id": reference, "revision": facts.mission_revision}}
                for cause, kind, reference in cls._admitted_evidence(facts)
            ),
        }

    @staticmethod
    def _admitted_evidence(facts: ClarificationFacts) -> tuple[tuple[str, str, str], ...]:
        if facts.active_run and not facts.workspace_patrol:
            return ()
        options = [
            ("human_gate", "gate", facts.pending_gate_ids),
            ("permission", "capability", facts.permission_needs),
            ("budget", "budget", facts.budget_exhaustions),
        ]
        if not facts.safe_continuation:
            options.extend((("missing_input", "input", facts.missing_input_ids), ("external_blocker", "external", facts.external_blockers)))
            if not facts.workspace_patrol and not (facts.outcome.strip() and facts.check_ids):
                options.append(("missing_goal", "mission", frozenset({"outcome"})))
        if facts.workspace_patrol:
            options.append(("missing_input", "input", facts.user_input_ids))
        return tuple(sorted((cause, kind, reference) for cause, kind, references in options for reference in references))

    @staticmethod
    def permission_needs(assessment: dict | None, capabilities: frozenset[str], permissions: frozenset[str]) -> frozenset[str]:
        opportunities = tuple((assessment or {}).get("opportunities") or ())
        needs = set()
        if opportunities and not {"create_lane", "spawn_context"}.intersection(capabilities):
            needs.add("create_lane")
        if "write" not in permissions and any(
            (item.get("work_spec") or {}).get("workspace_requirement") == "isolated_write"
            for item in opportunities
        ):
            needs.add("write")
        return frozenset(needs)

    def validate(self, action: WaitForUserAction, facts: ClarificationFacts) -> None:
        if action.cause == "awaiting_input" and facts.workspace_patrol:
            return
        if action.cause is None or action.evidence_identity is None or not action.required_input:
            raise ClarificationRejected("wait_for_user 必须声明类型化 cause、具体所需输入及当前证据 identity")
        evidence = action.evidence_identity
        if evidence.revision is not None and evidence.revision != facts.mission_revision:
            raise ClarificationRejected("wait_for_user evidence revision 已过期")
        if facts.active_run and not facts.workspace_patrol:
            raise ClarificationRejected("活动 Run 尚未结算，不得请求用户重述任务")
        if (action.cause, evidence.kind, evidence.reference_id) in self._admitted_evidence(facts):
            return
        if action.cause == "missing_goal":
            if facts.outcome.strip() and facts.check_ids:
                raise ClarificationRejected("当前 Mission 已包含最终结果与完成检查，不能声称缺少目标")
            raise ClarificationRejected("缺失目标等待缺少可核验的 Mission 前提")
        if action.cause == "missing_input":
            raise ClarificationRejected("缺失输入未绑定当前未决输入请求，无法证明必须等待用户")
        raise ClarificationRejected("wait_for_user cause 与当前 Mission、授权、gate、预算或外部证据不一致")
