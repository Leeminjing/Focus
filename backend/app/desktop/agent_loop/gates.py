r"""本文件对外提供 PendingDecisionProjector 与 PendingDecisionContract。

输入为 Commitment、Compression、must-view、access approval 或扩展 interrupt；输出为统一持久
pending-decision surface。具体工作流为安全默认不可委托，只有显式 allowlist 与 grant capability 同时
满足才标记 delegable；访问扩大永远要求用户。示例：`await projector.project(loop_id, interrupt)`。
"""

from __future__ import annotations

import hashlib
import json
import uuid

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopEventOutbox, LoopPendingDecision


class PendingDecisionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pending_decision_id: str
    kind: str
    delegable: bool
    payload: dict


class PendingDecisionProjector:
    SAFE_DELEGABLE = frozenset({"compression", "must_view"})

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def project(self, loop_id: str, interrupt: dict) -> PendingDecisionContract:
        payload = json.loads(json.dumps(interrupt, sort_keys=True, default=str))
        kind = str(payload.get("type") or "unknown")
        identity = hashlib.sha256(
            f"{loop_id}:".encode("utf-8")
            + json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        async with self._sessions.begin() as session:
            existing = await session.get(LoopPendingDecision, identity)
            if existing is not None:
                return self._contract(existing)
            grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active"))
            delegable = kind in self.SAFE_DELEGABLE and grant is not None and kind in set(grant.delegable_gates)
            row = LoopPendingDecision(pending_decision_id=identity, loop_id=loop_id, kind=kind, delegable=delegable, payload=payload)
            session.add(row)
            loop = await session.get(AgentLoop, loop_id, with_for_update=True)
            if loop is None:
                raise LookupError("pending decision 所属 Loop 不存在")
            if not delegable and loop.status in {"running", "paused"}:
                loop.status = "waiting_user"
                loop.health = "blocked"
                loop.waiting_reason = f"存在不可委托的 {kind} 决策，必须由用户处理"
            sequence = int(
                await session.scalar(
                    select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(
                        LoopEventOutbox.loop_id == loop_id
                    )
                ) or 0
            ) + 1
            session.add(
                LoopEventOutbox(
                    event_id=uuid.uuid4().hex,
                    loop_id=loop_id,
                    sequence=sequence,
                    event_type="LoopPendingDecisionProjected",
                    payload={
                        "pending_decision_id": identity,
                        "kind": kind,
                        "delegable": delegable,
                    },
                    idempotency_key=f"pending-decision:{identity}",
                )
            )
            return self._contract(row)

    @staticmethod
    def _contract(row: LoopPendingDecision) -> PendingDecisionContract:
        return PendingDecisionContract(pending_decision_id=row.pending_decision_id, kind=row.kind, delegable=row.delegable, payload=row.payload)
