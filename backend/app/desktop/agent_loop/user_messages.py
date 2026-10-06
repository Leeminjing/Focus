"""本文件对外提供 LoopUserMessageAdmission、message_request_hash 与 accepted_message_view。

输入为目标 Context、稳定请求身份和显式原始请求；输出为耐久用户消息受理身份或冲突，不产生 Run。
具体工作流为按请求身份串行化重放，锁定当前 Loop 和授权，验证角色装备，冻结原文及请求 hash，
推进控制版本并收敛旧工作，再创建观察轮；事务提交后通知取消。既有协调器读取此轮，不另建调度器。
示例：await admission.submit(context_id, request_id, {"message": "继续检查"})。
"""

from datetime import UTC, datetime
import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select

from backend.app.desktop.agent_loop.compression_authority.repository import CompressionAuthorityRepository
from backend.app.desktop.agent_loop.directive_equipment import resolve_context_equipment
from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.agent_loop.models import AgentLoop, LoopContextMembership, LoopDelegationGrant, LoopRound, LoopUserIntent
from backend.app.desktop.agent_loop.rounds import create_observation_round
from backend.app.desktop.agent_loop.runtime_convergence import LoopRuntimeConvergence


def message_request_hash(context_id: str, payload: dict) -> str:
    encoded = json.dumps({"context_id": context_id, "request": payload}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def accepted_message_view(intent: LoopUserIntent) -> dict:
    return {"intent_id": intent.intent_id, "loop_id": intent.loop_id, "context_id": intent.target_context_id,
            "intent_kind": intent.intent_kind, "origin": intent.origin_kind, "status": intent.status,
            "delivery_state": intent.delivery_state,
            "round_id": intent.observed_round_id or (intent.request_payload or {}).get("accepted_round_id"),
            "run_id": intent.resulting_run_id, "mission_revision": intent.goal_revision}


class LoopUserMessageAdmission:
    def __init__(self, sessions, append_event, notify_cancellation):
        self._sessions = sessions
        self._append_event = append_event
        self._notify_cancellation = notify_cancellation
        self._lifecycle = InterventionLifecycleRepository()
        self._convergence = LoopRuntimeConvergence()

    async def submit(self, context_id: str, request_id: str, payload: dict) -> dict | None:
        intent_id = uuid.uuid5(uuid.NAMESPACE_URL, f"focus:direct-user-intent:{request_id}").hex
        digest = message_request_hash(context_id, payload)
        run_ids = ()
        async with self._sessions.begin() as session:
            lock_key = int.from_bytes(hashlib.sha256(intent_id.encode()).digest()[:8], "big", signed=True)
            await session.execute(select(func.pg_advisory_xact_lock(lock_key)))
            existing = await session.get(LoopUserIntent, intent_id)
            if existing is not None:
                if existing.request_payload.get("request_hash") != digest:
                    raise HTTPException(409, "用户消息请求身份已用于不同目标或内容")
                return accepted_message_view(existing)
            loop = await self._loop(session, context_id)
            if loop is None:
                return None
            grant = await self._grant(session, loop, context_id)
            overrides = {key: value for key, value in payload.items()
                         if key in {"model_name", "skills", "memory_ids", "permissions", "access_mode"} and value is not None}
            try:
                await resolve_context_equipment(session, loop, context_id, overrides=overrides)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            loop.revision += 1
            loop.authority_revision += 1
            loop.status, loop.health, loop.waiting_reason = "running", "observing", None
            grant.status, grant.revoked_at = "revoked", datetime.now(UTC)
            session.add(self._renewed_grant(loop, grant))
            run_ids = await self._convergence.converge(session, loop, "direct_user_message")
            await CompressionAuthorityRepository().supersede(session, loop.loop_id, "direct_user_message")
            prior = await session.get(LoopRound, loop.current_round_id) if loop.current_round_id else None
            round_row = await create_observation_round(session, loop, prior, hashlib.sha256(context_id.encode()).hexdigest())
            loop.current_round_id = round_row.round_id
            original = payload["message"]
            content = original if isinstance(original, str) else json.dumps(original, ensure_ascii=False)
            intent = LoopUserIntent(intent_id=intent_id, loop_id=loop.loop_id, scope="context",
                target_context_id=context_id, content=content, intent_kind="direct_message", status="pending",
                request_payload={"request": payload, "request_hash": digest, "accepted_round_id": round_row.round_id},
                goal_revision=loop.goal_revision, authority_revision=loop.authority_revision, correlation_id=intent_id)
            session.add_all([round_row, intent])
            await self._lifecycle.register(session, intent)
            await self._lifecycle.transition(session, intent_id, "accepted")
            from backend.app.desktop.agent_loop.round_events import RoundStateEventRecorder
            from backend.app.desktop.agent_loop.lifecycle_events import LoopLifecycleEventRecorder

            await RoundStateEventRecorder().record(session, round_row)
            await LoopLifecycleEventRecorder().record(session, loop)
            await self._append_event(session, loop, "DirectUserMessageAccepted",
                {"intent_id": intent_id, "context_id": context_id, "round_id": round_row.round_id,
                 "mission_revision": loop.goal_revision})
            result = accepted_message_view(intent)
        self._notify_cancellation(run_ids)
        return result

    @staticmethod
    async def _loop(session, context_id):
        return await session.scalar(select(AgentLoop).join(LoopContextMembership,
            LoopContextMembership.loop_id == AgentLoop.loop_id).where(
            LoopContextMembership.context_id == context_id, LoopContextMembership.status.in_(("active", "paused")),
            AgentLoop.status.in_(("running", "paused", "waiting_user"))).order_by(
            AgentLoop.created_at.desc()).limit(1).with_for_update(of=AgentLoop))

    @staticmethod
    async def _grant(session, loop, context_id):
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active").with_for_update())
        if (grant is None or grant.revision != loop.authority_revision or context_id not in grant.context_scope
                or "continue_context" not in grant.capabilities
                or (grant.expires_at and grant.expires_at <= datetime.now(UTC))):
            raise HTTPException(409, "当前授权不接受该 Context 的用户消息")
        return grant

    @staticmethod
    def _renewed_grant(loop, grant):
        return LoopDelegationGrant(grant_id=uuid.uuid4().hex, loop_id=loop.loop_id,
            revision=loop.authority_revision, holder_id=grant.holder_id, capabilities=grant.capabilities,
            context_scope=grant.context_scope, permission_scope=grant.permission_scope, budgets=grant.budgets,
            delegable_gates=grant.delegable_gates, compression_policy=grant.compression_policy, expires_at=grant.expires_at)
