"""本文件对外提供 LoopUserMessageDelivery 的冻结消息校验与 Directive 适配。

输入为当前事务、真实 Patrol 决策、冻结 Observation 和 intent identity；输出为已核验目标或原文 Directive/provenance。
具体工作流为复核类型、正文请求 hash、Mission、membership、grant 和精确 Context revision，拒绝未冻结或重复交付；
Kernel 选择 identity，适配器从耐久原始请求生成 HumanMessage，模型不提供正文或装备。
示例：await delivery.validate(session, loop, round_row, grant, intent_id)，随后在同一 Kernel 事务创建 Directive。
"""

from langchain_core.messages import HumanMessage
from sqlalchemy import select

from backend.app.desktop.agent_loop.directive_equipment import resolve_context_equipment
from backend.app.desktop.agent_loop.mission_contract import ExecutionBoundaries
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.models import LoopDirective, LoopObservation, LoopUserIntent
from backend.app.desktop.agent_loop.provenance import DelegatedDirectiveFactory
from backend.app.desktop.agent_loop.user_messages import message_request_hash
from backend.app.desktop.models import DesktopThread


class UserMessageDeliveryRejected(ValueError):
    pass


class LoopUserMessageDelivery:
    async def validate(self, session, loop, round_row, grant, intent_id):
        intent = await session.get(LoopUserIntent, intent_id, with_for_update=True)
        observation = await session.get(LoopObservation, round_row.observation_id) if round_row.observation_id else None
        frozen = next((item for item in (observation.envelope.get("user_intents", ()) if observation else ())
                       if item.get("intent_id") == intent_id), None)
        if (intent is None or intent.intent_kind != "direct_message" or intent.loop_id != loop.loop_id
                or intent.delivery_state != "observed" or intent.status != "observed"
                or intent.observed_round_id != round_row.round_id or frozen is None):
            raise UserMessageDeliveryRejected("用户消息未在本轮冻结或已失去交付资格")
        payload = intent.request_payload
        digest = message_request_hash(intent.target_context_id, payload.get("request") or {})
        if (digest != payload.get("request_hash") or digest != frozen.get("request_hash")
                or frozen.get("context_id") != intent.target_context_id
                or frozen.get("intent_kind") != "direct_message" or intent.goal_revision != loop.goal_revision):
            raise UserMessageDeliveryRejected("用户消息目标、原文或 Mission 已变化")
        existing = await session.scalar(select(LoopDirective.directive_id).where(
            LoopDirective.origin_kind == "direct_user", LoopDirective.correlation_id == intent_id))
        if existing is not None:
            raise UserMessageDeliveryRejected("用户消息已有交付 Directive")
        target = await session.get(DesktopThread, intent.target_context_id)
        frontier = next((item for item in observation.envelope.get("portfolio_frontier", ())
                         if item.get("context_id") == intent.target_context_id), None)
        if (target is None or target.workspace_id != loop.workspace_id or frontier is None
                or target.current_revision_id != frontier.get("revision_id")
                or intent.target_context_id not in grant.context_scope):
            raise UserMessageDeliveryRejected("用户消息 Context revision 或授权已变化")
        await self._validate_boundaries(session, loop, intent.target_context_id)
        try:
            await resolve_context_equipment(session, loop, intent.target_context_id, overrides=payload.get("request"))
        except ValueError as exc:
            raise UserMessageDeliveryRejected(str(exc)) from exc
        return intent.target_context_id

    @staticmethod
    async def _validate_boundaries(session, loop, context_id):
        mission = await session.scalar(select(LoopMissionRevision).where(
            LoopMissionRevision.loop_id == loop.loop_id, LoopMissionRevision.revision == loop.goal_revision))
        if mission is None:
            return
        boundaries = ExecutionBoundaries.model_validate(mission.boundaries)
        if set(boundaries.blocked_action_types()) & {"continue_context", "deliver_user_message"}:
            raise UserMessageDeliveryRejected("Mission 禁止 Context 消息执行")
        contexts = set(boundaries.scoped_context_ids())
        if contexts and context_id not in contexts:
            raise UserMessageDeliveryRejected("用户消息目标超出 Mission 范围")

    @staticmethod
    async def create(session, loop, round_row, grant, decision, action, intent_id):
        intent = await session.get(LoopUserIntent, intent_id)
        target = await session.get(DesktopThread, intent.target_context_id)
        directive, provenance = DelegatedDirectiveFactory.create(loop_id=loop.loop_id,
            round_id=round_row.round_id, decision_id=decision.decision_id, action_id=action.action_id,
            context_id=intent.target_context_id, context_revision_id=target.current_revision_id,
            content=intent.content, actor_id=intent.intent_id, grant_id=grant.grant_id,
            grant_revision=grant.revision, goal_revision=loop.goal_revision,
            idempotency_key=f"user-message:{intent.intent_id}", origin_kind="direct_user", correlation_id=intent.intent_id)
        directive.actor_kind = "user"
        provenance.source_kind = "direct_user"
        provenance.audit = {**provenance.audit, "user_intent_id": intent.intent_id,
                            "request_hash": intent.request_payload["request_hash"]}
        action.result = {"user_intent_id": intent.intent_id, "request_hash": intent.request_payload["request_hash"]}
        return directive, provenance

    @staticmethod
    async def model_message(session, directive):
        intent = await session.get(LoopUserIntent, directive.correlation_id)
        if (intent is None or intent.intent_kind != "direct_message" or intent.loop_id != directive.loop_id
                or intent.target_context_id != directive.target_context_id or intent.content != directive.content
                or intent.request_payload.get("request_hash") != message_request_hash(
                    intent.target_context_id, intent.request_payload.get("request") or {})):
            raise UserMessageDeliveryRejected("用户消息交付来源不匹配")
        return HumanMessage(id=directive.message_id, content=intent.request_payload["request"]["message"])
