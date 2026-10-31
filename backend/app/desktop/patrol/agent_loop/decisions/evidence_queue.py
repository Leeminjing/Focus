"""Durable, Observation-bound preparation on the existing Worker pool.

The round releases its coordinator lease while indexing/Curators/assessment run.
Preparation and assessment have separate idempotent identities. Worker recovery,
bounded retries and observation preemption apply to both without a second queue.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from backend.app.desktop.patrol.agent_loop.decisions.decision_context import DecisionSupplementRepository, PatrolDecisionContext
from backend.app.desktop.patrol.agent_loop.decisions.evidence import PatrolEvidenceNeeds
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopObservation, LoopPatrolAttempt, LoopRound, LoopWorkerRequest
from backend.app.desktop.patrol.agent_loop.observations.builder import observation_hash
from backend.app.desktop.patrol.agent_loop.patrol import PatrolContractViolation
from backend.app.desktop.patrol.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.patrol.agent_loop.sessions.patrol_session_repository import PatrolSessionRepository
from backend.app.desktop.patrol.agent_loop.sessions.patrol_session_state import PatrolActivity, PatrolPhase, PatrolWaitTarget
from backend.app.desktop.patrol.agent_loop.workers.worker_attempts import WorkerAttemptAuthority


class PatrolEvidenceQueue:
    def __init__(self, sessions, curators, expansions, require_current):
        self._sessions = sessions
        self._curators = curators
        self._expansions = expansions
        self._require_current = require_current
        self._supplements = DecisionSupplementRepository()

    async def enqueue(self, claim, session_id, observation, needs: PatrolEvidenceNeeds):
        from backend.app.desktop.patrol.agent_loop.journal.lifecycle_events import LoopLifecycleEventRecorder
        from backend.app.desktop.patrol.agent_loop.journal.round_events import RoundStateEventRecorder

        identity = observation.observation_id or observation.decision_inputs_ref
        async with self._sessions.begin() as session:
            await self._require_current(session, claim, identity)
            curator = await self._supplements.get(session, identity, "curator_results")
            assessment = await self._supplements.get(session, identity, "expansion_assessment")
            if "expansion_assessment" in needs.needs_evidence and assessment is None:
                stage = "assessment" if curator is not None else "prepare"
            elif "curator" in needs.needs_evidence and curator is None:
                stage = "prepare"
            else:
                raise PatrolContractViolation("请求的证据已经可用，请依据已有证据决策")
            worker_id = uuid.uuid5(uuid.NAMESPACE_URL, f"patrol-evidence:{identity}:{stage}").hex
            existing = await session.get(LoopWorkerRequest, worker_id)
            if existing is not None:
                # Concurrent dispatch and crash recovery reuse the exact request.
                return worker_id
            session.add(LoopWorkerRequest(
                worker_request_id=worker_id, loop_id=observation.loop_id,
                round_id=observation.round_id, observation_id=identity,
                observation_generation=observation.generation,
                kind="patrol_evidence", scope={"stage": stage,
                    "needs_evidence": list(needs.needs_evidence), "patrol_session_id": session_id},
            ))
            await PatrolSessionRepository().transition(session, session_id,
                PatrolPhase.DISPATCHING_CURATORS if stage == "prepare" else PatrolPhase.AWAITING_EVIDENCE,
                PatrolActivity(summary="已排队准备所需证据", wait_reason="patrol_evidence",
                    wait_targets=(PatrolWaitTarget(entity_type="worker", entity_id=worker_id, evidence_category=stage),)),
                fencing_token=int(claim.fencing_token))
            round_row = await session.get(LoopRound, observation.round_id)
            loop = await session.get(AgentLoop, observation.loop_id)
            round_row.status = "waiting_workers"
            loop.health = "curating" if stage == "prepare" else "deciding"
            await RoundStateEventRecorder().record(session, round_row)
            await LoopLifecycleEventRecorder().record(session, loop)
            return worker_id

    async def assessment_requested(self, observation):
        async with self._sessions() as session:
            requests = await session.scalars(select(LoopWorkerRequest).where(
                LoopWorkerRequest.observation_id == (observation.observation_id or observation.decision_inputs_ref),
                LoopWorkerRequest.kind == "patrol_evidence"))
            return any("expansion_assessment" in row.scope.get("needs_evidence", ()) for row in requests)

    async def missing(self, observation, needs):
        async with self._sessions() as session:
            identity = observation.observation_id or observation.decision_inputs_ref
            missing = []
            for kind in needs.needs_evidence:
                if await self._supplements.get(session, identity,
                    "curator_results" if kind == "curator" else "expansion_assessment") is None:
                    missing.append(kind)
            return tuple(missing)

    async def recover_request(self, observation):
        # A crash after the model's durable receipt but before enqueue must not
        # lose its request or repeat an already charged cognitive call.
        async with self._sessions() as session:
            attempt = await session.scalar(select(LoopPatrolAttempt).where(
                LoopPatrolAttempt.observation_id == (observation.observation_id or observation.decision_inputs_ref))
                .order_by(LoopPatrolAttempt.attempt.desc()).limit(1))
            needs = PatrolEvidenceNeeds.model_validate({"needs_evidence": attempt.raw_output["needs_evidence"]}) \
                if attempt is not None and attempt.status == "waiting_evidence" else None
        if needs is not None and await self.missing(observation, needs):
            return needs
        return None

    async def run(self, request):
        async with self._sessions.begin() as session:
            await WorkerAttemptAuthority().require_current(session, request)
            base = await session.get(LoopObservation, request.observation_id)
            if base is None:
                raise ValueError("Patrol evidence 缺少不可变 Observation")
            observation = LoopObservationEnvelope.model_validate(base.envelope)
            if observation_hash(observation) != base.envelope_hash:
                raise ValueError("Patrol evidence Observation hash 不匹配")
            curator = await self._supplements.get(session, base.observation_id, "curator_results")
        if request.scope["stage"] == "prepare":
            await self._curators.dispatch(request.scope["patrol_session_id"], observation,
                self._curators.scopes(observation), worker_request=request)
        elif request.scope["stage"] == "assessment":
            if curator is None:
                raise ValueError("派生评估缺少本 Observation 的 Curator 证据")
            observation = PatrolDecisionContext(observation, curator_results=tuple(curator["results"])).model_observation()
            assessment = await self._expansions.assess(observation)
            async with self._sessions.begin() as session:
                await WorkerAttemptAuthority().require_current(session, request)
                await self._supplements.put(session, request.observation_id, "expansion_assessment", assessment.model_dump(mode="json"))
        else:
            raise ValueError("未知 Patrol evidence stage")
        return {"stage": request.scope["stage"], "observation_id": request.observation_id}
