r"""本文件对外提供 PatrolSessionLifecycle、CuratorCoordinationStage 与 PatrolOutcomeStage。

输入为 coordinator claim、冻结 observation、Kernel result 和持久 Session；输出为原子 phase 事件、单 Context Bootstrap 或
多 Context Cognitive Planner assignment、可供 Patrol 消费的结构化 work specs 及等待/终态。具体工作流为 Lifecycle 管理 Session 边界，
Curator stage 把冻结 Mission、带精确 unit identity 的 semantic manifests、Run/Workspace facts 按 Portfolio 形态有界扇出、
收集和消费，Outcome stage
只把 Kernel 结果映射为 delivery、waiting、publication 或终态。
示例：`handle = await lifecycle.begin(claim)`。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.context_expansion.semantic_manifest import (
    SemanticManifestProjector,
)
from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim
from backend.app.desktop.agent_loop.curator_assignments import (
    CuratorAssignmentRepository,
)
from backend.app.desktop.agent_loop.kernel import KernelCommitResult
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopRound,
    LoopWorkerRequest,
)
from backend.app.desktop.agent_loop.patrol_session_models import LoopPatrolSession
from backend.app.desktop.agent_loop.patrol_session_repository import (
    PatrolSessionRepository,
)
from backend.app.desktop.agent_loop.patrol_session_state import (
    PatrolActivity,
    PatrolPhase,
    PatrolWaitTarget,
)
from backend.app.desktop.agent_loop.schemas import (
    LoopObservationEnvelope,
    PatrolDecisionIntent,
)


@dataclass(frozen=True, slots=True)
class PatrolSessionHandle:
    session_id: str
    phase: str


class PatrolSessionLifecycle:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._repository = PatrolSessionRepository()

    async def begin(self, claim: CoordinatorClaim) -> PatrolSessionHandle:
        token = int(claim.fencing_token) if claim.fencing_token.isdecimal() else 0
        async with self._sessions.begin() as session:
            row = await self._repository.begin(session, claim.loop_id, claim.round_id, token)
            if row.current_phase == PatrolPhase.CREATED:
                row = await self._repository.transition(
                    session,
                    row.session_id,
                    PatrolPhase.FREEZING_OBSERVATION,
                    PatrolActivity(summary="正在冻结 Portfolio observation"),
                )
            return PatrolSessionHandle(row.session_id, row.current_phase)

    async def transition(
        self,
        session_id: str,
        target: PatrolPhase,
        activity: PatrolActivity,
        **references,
    ) -> PatrolSessionHandle:
        async with self._sessions.begin() as session:
            row = await self._repository.transition(session, session_id, target, activity, **references)
            return PatrolSessionHandle(row.session_id, row.current_phase)

    async def current(self, round_id: str) -> PatrolSessionHandle | None:
        async with self._sessions() as session:
            row = await session.scalar(select(LoopPatrolSession).where(LoopPatrolSession.round_id == round_id))
            return None if row is None else PatrolSessionHandle(row.session_id, row.current_phase)

    async def get(self, session_id: str) -> PatrolSessionHandle | None:
        async with self._sessions() as session:
            row = await session.get(LoopPatrolSession, session_id)
            return None if row is None else PatrolSessionHandle(row.session_id, row.current_phase)


class CuratorCoordinationStage:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], max_assignments: int = 8) -> None:
        self._sessions = sessions
        self._max_assignments = max(1, max_assignments)
        self._assignments = CuratorAssignmentRepository()
        self._sessions_repository = PatrolSessionRepository()

    def scopes(self, observation: LoopObservationEnvelope) -> tuple[dict, ...]:
        eligible = tuple(item for item in observation.portfolio_frontier if item.get("revision_id"))
        if not eligible:
            return ()
        mode = "bootstrap" if len(eligible) == 1 else "lane"
        return tuple(
            {
                "mode": mode,
                "lane_id": item.get("lane_id"),
                "context_id": item["context_id"],
                "revision_id": item["revision_id"],
                "role": item.get("role"),
            }
            for item in eligible[: self._max_assignments]
        )

    async def dispatch(self, session_id: str, observation: LoopObservationEnvelope, scopes: tuple[dict, ...]) -> int:
        async with self._sessions.begin() as session:
            existing = await self._assignments.by_session(session, session_id)
            if not existing:
                patrol = await session.get(LoopPatrolSession, session_id, with_for_update=True)
                if patrol is None:
                    raise LookupError("Patrol session 不存在")
                for scope in scopes:
                    request_id = uuid.uuid4().hex
                    request = LoopWorkerRequest(
                        worker_request_id=request_id,
                        loop_id=patrol.loop_id,
                        round_id=patrol.round_id,
                        kind="lane_curator",
                        scope={
                            "assignments": [scope],
                            "curator_mode": scope["mode"],
                            "patrol_session_id": session_id,
                            "derivation_input": self._derivation_input(observation),
                        },
                    )
                    session.add(request)
                    await session.flush()
                    await self._assignments.create(
                        session,
                        assignment_key=str(scope.get("lane_id") or scope["context_id"]),
                        session_id=session_id,
                        loop_id=patrol.loop_id,
                        round_id=patrol.round_id,
                        worker_request_id=request_id,
                        scope=scope,
                    )
                round_row = await session.get(LoopRound, patrol.round_id, with_for_update=True)
                loop = await session.get(AgentLoop, patrol.loop_id, with_for_update=True)
                if round_row is not None:
                    round_row.status = "waiting_workers"
                if loop is not None:
                    loop.health = "curating"
                existing = await self._assignments.by_session(session, session_id)
            patrol = await session.get(LoopPatrolSession, session_id, with_for_update=True)
            if patrol is not None and patrol.current_phase == PatrolPhase.DISPATCHING_CURATORS:
                await self._sessions_repository.transition(
                    session,
                    session_id,
                    PatrolPhase.COLLECTING_CURATORS,
                    PatrolActivity(
                        summary=f"已分派 {len(existing)} 个 Curator，正在收集结果",
                        wait_reason="curator_results",
                        wait_targets=tuple(PatrolWaitTarget(entity_type="curator", entity_id=item.assignment_id, evidence_category="proposal") for item in existing),
                    ),
                )
            return len(existing)

    @staticmethod
    def _derivation_input(observation: LoopObservationEnvelope) -> dict:
        manifests = SemanticManifestProjector().project(observation)
        return {
            "mission": observation.mission or observation.goal,
            "portfolio_frontier": observation.portfolio_frontier,
            "semantic_manifests": tuple(item.model_dump(mode="json") for item in manifests),
            "stable_results": observation.stable_results,
            "user_intents": observation.user_intents,
            "workspace": observation.workspace,
            "budget": observation.budget,
            "context_scope": tuple((observation.grant or {}).get("context_scope") or ()),
        }

    async def results(self, session_id: str) -> tuple[dict, ...]:
        async with self._sessions() as session:
            assignments = await self._assignments.by_session(session, session_id)
            results = []
            for assignment in assignments:
                worker = await session.get(LoopWorkerRequest, assignment.worker_request_id)
                results.append(
                    {
                        "assignment_id": assignment.assignment_id,
                        "request_id": assignment.worker_request_id,
                        "kind": "lane_curator",
                        "scope": assignment.scope,
                        "status": assignment.state,
                        "result": worker.result if worker is not None else {},
                    }
                )
            return tuple(results)

    async def consume(self, session_id: str) -> int:
        async with self._sessions.begin() as session:
            assignments = await self._assignments.by_session(session, session_id)
            consumed = 0
            for assignment in assignments:
                if assignment.state == "proposed":
                    await self._assignments.transition(
                        session,
                        assignment.assignment_id,
                        "consumed",
                        "Patrol 已消费 Curator proposal",
                    )
                    consumed += 1
            return consumed


class PatrolOutcomeStage:
    def __init__(self, lifecycle: PatrolSessionLifecycle) -> None:
        self._lifecycle = lifecycle

    async def apply(self, session_id: str, intent: PatrolDecisionIntent, result: KernelCommitResult) -> None:
        current = await self._lifecycle.get(session_id)
        if current is None or current.phase in {
            PatrolPhase.COMPLETED,
            PatrolPhase.FAILED,
            PatrolPhase.INTERRUPTED,
            PatrolPhase.SUPERSEDED,
        }:
            return
        if result.status == "publishing":
            if current.phase == PatrolPhase.AUTHORIZING:
                await self._lifecycle.transition(session_id, PatrolPhase.PUBLISHING, PatrolActivity(summary="Portfolio publication 已排队"))
            return
        if result.status == "superseded":
            await self._lifecycle.transition(session_id, PatrolPhase.SUPERSEDED, PatrolActivity(summary="Patrol decision 已被新权威状态取代"), terminal_outcome={"status": "superseded", "reason": result.reason})
            return
        if result.status != "committed":
            await self._lifecycle.transition(session_id, PatrolPhase.FAILED, PatrolActivity(summary="Kernel 拒绝了 Patrol decision"), terminal_outcome={"status": "failed", "reason": result.reason})
            return
        if result.directive_ids:
            await self._lifecycle.transition(session_id, PatrolPhase.DELIVERING, PatrolActivity(summary=f"已授权 {len(result.directive_ids)} 条 Context directive"))
            await self._lifecycle.transition(session_id, PatrolPhase.AWAITING_EVIDENCE, PatrolActivity(summary="正在等待 Context Run 证据", wait_reason="context_run_evidence", wait_targets=tuple(PatrolWaitTarget(entity_type="directive", entity_id=item, evidence_category="run") for item in result.directive_ids)))
            return
        if any(action.action.startswith("request_") for action in intent.actions):
            await self._lifecycle.transition(session_id, PatrolPhase.AWAITING_EVIDENCE, PatrolActivity(summary="正在等待请求的独立证据", wait_reason="worker_evidence"))
            return
        await self._lifecycle.transition(session_id, PatrolPhase.COMPLETED, PatrolActivity(summary="Patrol round 已完成"), terminal_outcome={"status": "completed", "decision_id": result.decision_id})
