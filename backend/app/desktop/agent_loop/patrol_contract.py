r"""本文件对外提供 Patrol 决策合同：MissionReference 封闭引用、OutcomeReference、BoundaryReference、CompletionCheckReference 与 PatrolDecisionContract。

输入为模型返回的 action 序列、mission 引用序列与冻结的 LoopObservationEnvelope（含 Mission、expansion 与 recovery
assessment）；输出为合同通过，或携带稳定拒绝原因的 PatrolContractViolation。具体工作流为先用封闭类型声明
不可枚举之外的引用取值（outcome 固定 identity、execution boundary 分组固定枚举且以中文列出合法分组），使其随 schema 对模型可见；
再校验 completion check 的动态合法集合（当前 Mission revision 的稳定 check identity）；最后校验派生与恢复决策：
派生只能按 identity 选择冻结 opportunity，拒绝必须引用策略已登记的 blocker，且 required 评估必须给出
派生、策略允许的拒绝或交回用户三者之一；单来源恢复只能引用 observation 中已持久化的 opportunity identity；
澄清动作还要通过共享准入策略核对冻结的 Mission、授权、gate、预算和可安全继续的 Context。

示例：`PatrolDecisionContract().validate(actions=proposal.actions, mission_references=proposal.mission_references, observation=observation)`。
消息交付只允许选本轮冻结的唯一 intent identity；未处理直接消息阻止完成提案，模型不能改写正文。
完成重请求复用冻结的 CompletionRequestAdmission，等价输入反馈进入原有有界候选纠错，最终 Kernel 仍复检。
完成动作精确选择 completion_eligibility 冻结目录中的可用验证，拒绝码进入同一纠错流程，不把新验证准入当完成许可。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.desktop.agent_loop.clarification_admission import ClarificationAdmissionPolicy, ClarificationFacts, ClarificationRejected
from backend.app.desktop.agent_loop.patrol import PatrolContractViolation
from backend.app.desktop.agent_loop.schemas import DeclineExpansionAction, LoopObservationEnvelope, PatrolModelAction, RecoverContextAction, SpawnContextAction, WaitForUserAction


BoundaryGroup = Literal["in_scope", "required_invariants", "prohibited_actions", "legacy_text"]
_BOUNDARY_GROUPS = ("in_scope", "required_invariants", "prohibited_actions", "legacy_text")
_EXPANSION_ACTIONS = frozenset({"spawn_context", "decline_expansion"})
_EXPANSION_EXITS = "spawn_context（策略允许的派生）、引用策略已登记 blocker 的 decline_expansion，或 wait_for_user"


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OutcomeReference(_ContractModel):
    """指向最终结果的 Mission 引用；identity 固定，模型无法自选取值。"""

    role: Literal["outcome"] = "outcome"
    reference_id: Literal["outcome"] = "outcome"


class BoundaryReference(_ContractModel):
    """指向 execution boundary 分组的 Mission 引用；分组取值为封闭枚举。"""

    role: Literal["boundary"] = "boundary"
    reference_id: BoundaryGroup

    @field_validator("reference_id", mode="before")
    @classmethod
    def require_declared_group(cls, value: Any) -> Any:
        if value not in _BOUNDARY_GROUPS:
            raise ValueError("boundary 引用必须是已声明分组之一：" + ", ".join(_BOUNDARY_GROUPS))
        return value


class CompletionCheckReference(_ContractModel):
    """指向完成检查的 Mission 引用；合法集合由当前 Mission revision 动态决定，故此处只约束形状。"""

    role: Literal["completion_check"] = "completion_check"
    reference_id: str = Field(min_length=1, max_length=96)


MissionReference = Annotated[
    OutcomeReference | BoundaryReference | CompletionCheckReference,
    Field(discriminator="role"),
]


class PatrolDecisionContract:
    """校验一次 Patrol 决策是否满足 mission 引用与派生决策合同。"""

    def validate(
        self,
        *,
        actions: tuple[PatrolModelAction, ...],
        mission_references: tuple[MissionReference, ...],
        observation: LoopObservationEnvelope,
    ) -> None:
        self._validate_mission_references(mission_references, observation)
        self._validate_expansion_decision(actions, observation)
        self._validate_recovery_decision(actions, observation)
        self._validate_clarification(actions, observation)
        self._validate_user_messages(actions, observation)
        from backend.app.desktop.agent_loop.completion_eligibility import CompletionEligibilityPolicy, CompletionEligibilityRejected

        for action in actions:
            if action.action == 'request_completion':
                try:
                    CompletionEligibilityPolicy.require_allowed(observation.completion_eligibility, action)
                except CompletionEligibilityRejected as exc:
                    raise PatrolContractViolation(str(exc)) from exc
        if any(action.action == 'request_completion_verifier' for action in actions) and observation.completion_admission is not None:
            from backend.app.desktop.agent_loop.completion_admission import CompletionRequestAdmission

            try:
                CompletionRequestAdmission.require_allowed(observation.completion_admission)
            except ValueError as exc:
                raise PatrolContractViolation(str(exc)) from exc

    @staticmethod
    def _validate_user_messages(actions, observation):
        messages = {item["intent_id"] for item in observation.user_intents
                    if item.get("intent_kind") == "direct_message"}
        selected = [action.intent_id for action in actions if action.action == "deliver_user_message"]
        if len(selected) != len(set(selected)) or not set(selected).issubset(messages):
            raise PatrolContractViolation("用户消息交付只能选择本轮冻结的唯一 intent identity")
        if messages and any(action.action == "request_completion" for action in actions):
            raise PatrolContractViolation("未处理直接用户消息阻止完成")

    @staticmethod
    def _validate_clarification(actions: tuple[PatrolModelAction, ...], observation: LoopObservationEnvelope) -> None:
        waits = tuple(action for action in actions if isinstance(action, WaitForUserAction))
        if not waits:
            return
        facts = ClarificationFacts.from_observation(observation)
        for action in waits:
            try:
                ClarificationAdmissionPolicy().validate(action, facts)
            except ClarificationRejected as exc:
                raise PatrolContractViolation(str(exc)) from exc

    def _validate_mission_references(
        self,
        mission_references: tuple[MissionReference, ...],
        observation: LoopObservationEnvelope,
    ) -> None:
        mission = observation.mission or {}
        check_ids = tuple(str(item.get("check_id")) for item in mission.get("completion_checks", ()) if item.get("check_id"))
        for reference in mission_references:
            if reference.role != "completion_check":
                continue
            if reference.reference_id not in check_ids:
                raise PatrolContractViolation(
                    "completion Mission 引用不是当前 revision 的稳定 check_id；合法集合为 " + (", ".join(check_ids) or "空")
                )

    def _validate_expansion_decision(
        self,
        actions: tuple[PatrolModelAction, ...],
        observation: LoopObservationEnvelope,
    ) -> None:
        assessment = observation.expansion_assessment or {}
        opportunities = self._opportunity_ids(assessment)
        blockers = self._blockers(assessment)
        expansion_actions = tuple(action for action in actions if action.action in _EXPANSION_ACTIONS)
        for action in expansion_actions:
            if action.action == "spawn_context":
                self._validate_spawn(action, opportunities)
            else:
                self._validate_decline(action, blockers)
        if assessment.get("level") == "required" and not self._has_required_exit(actions, expansion_actions):
            raise PatrolContractViolation("required Context expansion 必须提供合法出口：" + _EXPANSION_EXITS)

    @staticmethod
    def _validate_recovery_decision(
        actions: tuple[PatrolModelAction, ...],
        observation: LoopObservationEnvelope,
    ) -> None:
        opportunities = {
            str(item.get("opportunity_id"))
            for item in getattr(observation, "recovery_opportunities", ())
            if item.get("opportunity_id")
        }
        for action in actions:
            if isinstance(action, RecoverContextAction) and action.opportunity_id not in opportunities:
                raise PatrolContractViolation("recover_context 引用了当前 observation 之外的 opportunity")

    @staticmethod
    def _validate_spawn(action: SpawnContextAction, opportunities: frozenset[str]) -> None:
        if action.opportunity_id not in opportunities:
            raise PatrolContractViolation("spawn_context 引用了当前 assessment 之外的 opportunity")

    @staticmethod
    def _validate_decline(action: DeclineExpansionAction, blockers: frozenset[tuple[str | None, str]]) -> None:
        if (action.opportunity_id, action.blocker_code) not in blockers:
            raise PatrolContractViolation("decline_expansion 必须引用当前策略允许的 blocker")

    @staticmethod
    def _has_required_exit(actions: tuple[PatrolModelAction, ...], expansion_actions: tuple[PatrolModelAction, ...]) -> bool:
        if expansion_actions:
            return True
        return any(action.action == "wait_for_user" for action in actions)

    @staticmethod
    def _opportunity_ids(assessment: dict[str, Any]) -> frozenset[str]:
        return frozenset(str(item.get("opportunity_id")) for item in assessment.get("opportunities") or () if item.get("opportunity_id"))

    @staticmethod
    def _blockers(assessment: dict[str, Any]) -> frozenset[tuple[str | None, str]]:
        return frozenset(
            (item.get("opportunity_id"), str(item.get("code")))
            for item in assessment.get("blockers") or ()
            if item.get("code")
        )
