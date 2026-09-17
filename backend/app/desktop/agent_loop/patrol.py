r"""本文件对外提供 PortfolioPatrol、PatrolDecisionModel 与 PatrolContractViolation。

输入为一个不可变 bounded LoopObservationEnvelope、当前 holder/grant 身份和独立模型调用；输出为恰好
一个 PatrolDecisionIntent，或携带模型原始输出的可重试合同违例。具体工作流为每轮创建隔离 attempt
identity，模型可自行判断并可选择请求 Worker，结果先持久化为 proposal，再由 Kernel commit；模型回答
形状不合法时抛出 PatrolContractViolation，attempt 记为 error 并保留原始输出，是否重试由调用方决定。
示例：`result = await patrol.decide(envelope, identity)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Awaitable, Callable, Protocol
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import LoopPatrolAttempt
from backend.app.desktop.agent_loop.observation import observation_hash
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope, PatrolDecisionIntent


class PatrolDecisionModel(Protocol):
    async def __call__(self, observation: LoopObservationEnvelope) -> PatrolDecisionIntent: ...


class PatrolContractViolation(RuntimeError):
    """模型回答不符合 Patrol 认知步骤合同；携带原始输出供审计与重试判断。"""

    def __init__(self, message: str, raw_output: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output


class PortfolioPatrol:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], model: PatrolDecisionModel) -> None:
        self._sessions = sessions
        self._model = model

    async def decide(self, observation: LoopObservationEnvelope, holder_id: str) -> PatrolDecisionIntent:
        attempt = await self._begin(observation, holder_id)
        try:
            intent = await self._model(observation)
            self._validate_output(intent, observation, holder_id)
        except Exception as exc:
            raw_output = getattr(exc, "raw_output", None)
            await self._finish(attempt, "error", {"raw_text": raw_output} if raw_output else {}, str(exc))
            raise
        await self._finish(attempt, "success", intent.model_dump(mode="json"), None)
        return intent

    async def _begin(self, observation: LoopObservationEnvelope, holder_id: str) -> LoopPatrolAttempt:
        async with self._sessions.begin() as session:
            count = int(await session.scalar(select(func.count()).select_from(LoopPatrolAttempt).where(LoopPatrolAttempt.round_id == observation.round_id)) or 0)
            row = LoopPatrolAttempt(patrol_attempt_id=uuid.uuid4().hex, loop_id=observation.loop_id, round_id=observation.round_id, attempt=count + 1, execution_thread_id=f"{observation.loop_id}:patrol:{observation.round_id}:{count + 1}", checkpoint_ns=f"loop-patrol:{holder_id}", observation_hash=observation_hash(observation), status="running")
            session.add(row)
            return row

    async def _finish(self, attempt: LoopPatrolAttempt, status: str, output: dict, error: str | None) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(LoopPatrolAttempt, attempt.patrol_attempt_id, with_for_update=True)
            row.status = status
            row.raw_output = output
            row.error = error
            row.completed_at = datetime.now(UTC)

    @staticmethod
    def _validate_output(intent: PatrolDecisionIntent, observation: LoopObservationEnvelope, holder_id: str) -> None:
        if intent.loop_id != observation.loop_id or intent.round_id != observation.round_id:
            raise ValueError("Patrol decision 未绑定当前 Loop/round")
        if intent.holder_id != holder_id:
            raise ValueError("Patrol decision holder 不匹配")
        if intent.loop_revision != observation.loop_revision:
            raise ValueError("Patrol decision Loop revision 不匹配")
        if intent.goal_revision != observation.goal_revision:
            raise ValueError("Patrol decision goal revision 不匹配")
