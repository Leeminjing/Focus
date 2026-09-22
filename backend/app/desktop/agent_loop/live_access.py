r"""本文件对外提供 LiveAccess、LoopLiveAccessPolicy 与 LoopLiveRedactionPolicy。

输入为 Loop identity、当前 delegation grant、projection 和权限集合；输出为当前授权快照或递归脱敏后的 Live projection。
具体工作流为读取最新 grant 建立访问边界，再从所有实体 state 移除秘密、隐藏推理和无权证据；示例：`access = await policy.resolve(session, loop_id)`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.live_projection_contract import LoopLiveProjection, ProjectedEntity
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant


@dataclass(frozen=True, slots=True)
class LiveAccess:
    authority_revision: int
    permissions: frozenset[str]


class LoopLiveAccessPolicy:
    async def resolve(self, session: AsyncSession, loop_id: str) -> LiveAccess:
        loop = await session.get(AgentLoop, loop_id)
        if loop is None:
            raise HTTPException(404, "Agent Loop 不存在")
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id).order_by(LoopDelegationGrant.revision.desc()).limit(1))
        return LiveAccess(loop.authority_revision, frozenset(grant.permission_scope if grant is not None else ()))


class LoopLiveRedactionPolicy:
    @classmethod
    def apply(cls, projection: LoopLiveProjection, permissions: frozenset[str]) -> LoopLiveProjection:
        def redact(entity: ProjectedEntity | None) -> ProjectedEntity | None:
            if entity is None:
                return None
            return entity.model_copy(update={"state": cls._value(entity.state, permissions)})
        return projection.model_copy(update={
            "loop": redact(projection.loop),
            "mission": redact(projection.mission),
            "patrol_session": redact(projection.patrol_session),
            "round": redact(projection.round),
            "portfolio": redact(projection.portfolio),
            "contexts": {key: redact(value) for key, value in projection.contexts.items()},
            "lineage": {key: redact(value) for key, value in projection.lineage.items()},
            "runs": {key: redact(value) for key, value in projection.runs.items()},
            "curators": {key: redact(value) for key, value in projection.curators.items()},
            "directives": {key: redact(value) for key, value in projection.directives.items()},
            "facts": {key: redact(value) for key, value in projection.facts.items()},
            "wait_requests": {key: redact(value) for key, value in projection.wait_requests.items()},
            "wait_responses": {key: redact(value) for key, value in projection.wait_responses.items()},
        })

    @classmethod
    def _value(cls, value: Any, permissions: frozenset[str]) -> Any:
        if isinstance(value, dict):
            hidden = {"chain_of_thought", "private_reasoning", "raw_prompt", "reasoning_content", "secret", "token", "tool_arguments"}
            if "view_evidence" not in permissions:
                hidden |= {"evidence", "evidence_references", "workspace_result"}
            return {key: cls._value(item, permissions) for key, item in value.items() if key.casefold() not in hidden}
        if isinstance(value, list):
            return [cls._value(item, permissions) for item in value]
        return value
