r"""本文件对外提供 PortfolioPatrol、PatrolDecisionModel 与 PatrolContractViolation。

输入为一个不可变 bounded LoopObservationEnvelope、当前 holder/grant 身份和独立模型调用；输出为恰好
一个 PatrolDecisionIntent，或携带有界原始输出及可选已解析动作的可重试合同违例。具体工作流为每轮创建隔离 attempt
identity，记录基础 Observation hash 与独立组合输入 hash；模型可自行判断并可选择请求 Worker，结果先持久化为 proposal，再由 Kernel commit；模型回答
形状不合法时抛出 PatrolContractViolation，attempt 记为 error 并保留原始输出；已解析动作通过 audit 纯投影保存无正文诊断，是否重试由调用方决定。
模型请求来源摘要与可用的实际用量附在现有 attempt 审计中，opaque continuation 不进入日志或任务事实。
示例：`result = await patrol.decide(envelope, identity)`。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import LoopObservation, LoopPatrolAttempt
from backend.app.desktop.agent_loop.observation import observation_hash
from backend.app.desktop.agent_loop.patrol_audit import proposal_failure_diagnostic
from backend.app.desktop.agent_loop.schemas import (
    LoopObservationEnvelope,
    PatrolDecisionIntent,
    PatrolModelAction,
)


class PatrolDecisionModel(Protocol):
    async def __call__(self, observation: LoopObservationEnvelope) -> PatrolDecisionIntent: ...


class PatrolContractViolation(RuntimeError):
    def __init__(self, message: str, raw_output: str | None = None, *, proposal_actions: tuple[PatrolModelAction, ...] | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output
        self.proposal_actions = proposal_actions


class PortfolioPatrol:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], model: PatrolDecisionModel) -> None:
        self._sessions = sessions
        self._model = model

    async def decide(self, observation: LoopObservationEnvelope, holder_id: str) -> PatrolDecisionIntent:
        attempt = await self._begin(observation, holder_id)
        if hasattr(self._model, "bind_usage_receipts"):
            from backend.app.desktop.agent_loop.model_usage_owner import OwnedModelUsage

            self._model.bind_usage_receipts(OwnedModelUsage(self._sessions, observation.loop_id, observation.round_id,
                                                          "patrol", attempt.patrol_attempt_id))
        try:
            intent = await self._model(observation)
            self._validate_output(intent, observation, holder_id)
        except BaseException as exc:
            raw_output = getattr(exc, "raw_output", None)
            output = {"raw_text": raw_output} if raw_output else {}
            actions = getattr(exc, "proposal_actions", None)
            if actions is not None:
                output["proposal_diagnostic"] = proposal_failure_diagnostic(actions)
            await self._finish(attempt, "error", output, str(exc))
            raise
        await self._finish(attempt, "success", intent.model_dump(mode="json"), None)
        return intent

    async def _begin(self, observation: LoopObservationEnvelope, holder_id: str) -> LoopPatrolAttempt:
        async with self._sessions.begin() as session:
            count = int(await session.scalar(select(func.count()).select_from(LoopPatrolAttempt).where(LoopPatrolAttempt.round_id == observation.round_id)) or 0)
            row = LoopPatrolAttempt(patrol_attempt_id=uuid.uuid4().hex, loop_id=observation.loop_id, round_id=observation.round_id, attempt=count + 1, execution_thread_id=f"{observation.loop_id}:patrol:{observation.round_id}:{count + 1}", checkpoint_ns=f"loop-patrol:{holder_id}", observation_hash=observation_hash(observation), status="running")
            base = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == observation.round_id))
            if base is not None:
                row.observation_hash = base.envelope_hash
            row.raw_output = {"decision_context": {"base_observation_id": base.observation_id if base else None, "base_hash": row.observation_hash, "composed_hash": observation_hash(observation)}}
            session.add(row)
            return row

    async def _finish(self, attempt: LoopPatrolAttempt, status: str, output: dict, error: str | None) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(LoopPatrolAttempt, attempt.patrol_attempt_id, with_for_update=True)
            if row.status not in {"interrupted", "cancelled"}:
                row.status = status
            row.raw_output = {**(row.raw_output or {}), **output,
                              "model_attempts": list(getattr(self._model, "attempt_metadata", ())) }
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
