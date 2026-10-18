"""按 Round 当前或精确 Observation 读取 Patrol 审计，复用当前 Live 访问和脱敏规则。

输入为 Loop、可选 Round/Observation；输出为所选版本身份、阶段与 Curator 记录。
缺省读取当前 Round，精确版本只读取所属 Session；没有 Session 时保留所选身份并返回空历史。
只读，不从当前运行状态回填历史。示例：await PatrolAuditQuery().read(session, loop_id)。
"""

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.live_access import LoopLiveAccessPolicy, LoopLiveRedactionPolicy
from backend.app.desktop.agent_loop.models import AgentLoop, LoopObservation, LoopRound
from backend.app.desktop.agent_loop.patrol_audit import PatrolAuditRepository


class PatrolAuditQuery:
    async def read(
        self, session: AsyncSession, loop_id: str, *,
        round_id: str | None = None, observation_id: str | None = None,
    ) -> dict:
        access = await LoopLiveAccessPolicy().resolve(session, loop_id)
        if observation_id is not None:
            observation = await session.get(LoopObservation, observation_id)
            if observation is None or observation.loop_id != loop_id:
                raise HTTPException(404, "Patrol 审计 Observation 不存在")
            if round_id is not None and observation.round_id != round_id:
                raise HTTPException(404, "Patrol 审计 Observation 不属于指定 Round")
            round_id = observation.round_id
        if round_id is None:
            loop = await session.get(AgentLoop, loop_id)
            round_id = loop.current_round_id
        round_row = await session.get(LoopRound, round_id, populate_existing=True) if round_id else None
        if round_id is not None and (round_row is None or round_row.loop_id != loop_id):
            raise HTTPException(404, "Patrol 审计 Round 不属于指定 Loop")
        selected_id = observation_id
        if selected_id is None and round_row is not None:
            selected_id = round_row.observation_id
        try:
            history = await PatrolAuditRepository().round_history(
                session, loop_id, round_id, observation_id=selected_id,
            ) if selected_id is not None else {"session": None, "phases": [], "curators": []}
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        selected = await session.get(LoopObservation, selected_id) if selected_id else None
        # Repository 返回 tuple；先转换成 JSON 容器，让递归脱敏覆盖每条历史记录。
        payload = jsonable_encoder({
            "loop_id": loop_id, "round_id": round_id,
            "observation_id": selected_id,
            "observation_generation": selected.generation if selected else None,
            **history,
        })
        if "view_evidence" not in access.permissions:
            for item in payload["phases"] + payload["curators"]:
                item.pop("evidence_refs", None)
        return LoopLiveRedactionPolicy.redact_value(payload, access.permissions)
