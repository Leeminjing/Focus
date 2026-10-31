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
from backend.app.desktop.patrol.agent_loop.decisions.evidence_jobs import EvidenceJobRepository, source_revisions
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
            required = source_revisions(observation, needs.context_ids)
            results = await self._supplements.curator_results(session, observation)
            available = {key for item in results for key in item["required_source_revisions"]}
            assessment = await self._supplements.assessment(session, observation, needs.context_ids)
            missing_contexts = sorted(required.keys() - available)
            if "expansion_assessment" in needs.needs_evidence and assessment is None and not missing_contexts:
                stage = "assessment"
                batches = [tuple(required)]
            elif missing_contexts:
                stage = "prepare"
                batches = [(context_id,) for context_id in missing_contexts]
            else:
                raise PatrolContractViolation("请求的证据已经可用，请依据已有证据决策")
            worker_ids = []
            for context_ids in batches:
                kind = "context_index" if stage == "prepare" else "expansion_assessment"
                job_identity = source_revisions(observation, context_ids)
                from backend.app.desktop.patrol.agent_loop.task_progress.contracts import canonical_hash

                worker_id = uuid.uuid5(uuid.NAMESPACE_URL,
                    f"patrol-evidence:{identity}:{observation.generation}:{stage}:{canonical_hash(job_identity)}").hex
                worker_ids.append(worker_id)
                existing = await session.get(LoopWorkerRequest, worker_id)
                if existing is not None:
                    continue
                session.add(LoopWorkerRequest(
                    worker_request_id=worker_id, loop_id=observation.loop_id,
                    round_id=observation.round_id, observation_id=identity,
                    observation_generation=observation.generation,
                    kind="patrol_evidence", scope={"stage": stage, "context_ids": list(context_ids),
                        "requested_context_ids": list(needs.context_ids),
                        "needs_evidence": list(needs.needs_evidence), "patrol_session_id": session_id},
                ))
                await session.flush()
                await EvidenceJobRepository().ensure(session, observation, kind, context_ids, worker_id)
            # Repeated enqueue is an idempotent recovery path, including when a
            # Context already dispatched its Curator but siblings are preparing.
            from backend.app.desktop.patrol.agent_loop.sessions.patrol_session_models import LoopPatrolSession
            patrol = await session.get(LoopPatrolSession, session_id)
            target = (PatrolPhase.DISPATCHING_CURATORS if stage == "prepare" else PatrolPhase.AWAITING_EVIDENCE)
            if patrol.current_phase in {PatrolPhase.DISPATCHING_CURATORS, PatrolPhase.COLLECTING_CURATORS}:
                target = PatrolPhase(patrol.current_phase)
            if patrol.current_phase != PatrolPhase.DISPATCHING_CURATORS:
                await PatrolSessionRepository().transition(session, session_id, target,
                    PatrolActivity(summary="已排队准备所需证据", wait_reason="patrol_evidence",
                        wait_targets=tuple(PatrolWaitTarget(entity_type="worker", entity_id=item, evidence_category=stage) for item in worker_ids)),
                    fencing_token=int(claim.fencing_token))
            round_row = await session.get(LoopRound, observation.round_id)
            loop = await session.get(AgentLoop, observation.loop_id)
            round_row.status = "waiting_workers"
            loop.health = "curating" if stage == "prepare" else "deciding"
            await RoundStateEventRecorder().record(session, round_row)
            await LoopLifecycleEventRecorder().record(session, loop)
            return worker_ids[0]

    async def assessment_requested(self, observation):
        async with self._sessions() as session:
            requests = await session.scalars(select(LoopWorkerRequest).where(
                LoopWorkerRequest.observation_id == (observation.observation_id or observation.decision_inputs_ref),
                LoopWorkerRequest.kind == "patrol_evidence"))
            requested = tuple(row for row in requests if "expansion_assessment" in row.scope.get("needs_evidence", ()))
            if not requested:
                return None
            contexts = () if any(not row.scope.get("requested_context_ids") for row in requested) else tuple(sorted({
                context_id for row in requested for context_id in row.scope["requested_context_ids"]}))
            return PatrolEvidenceNeeds(needs_evidence=("expansion_assessment",), context_ids=contexts)

    async def preparation_enqueued(self, observation, session_id):
        async with self._sessions() as session:
            return await session.scalar(select(LoopWorkerRequest.worker_request_id).where(
                LoopWorkerRequest.observation_id == (observation.observation_id or observation.decision_inputs_ref),
                LoopWorkerRequest.observation_generation == observation.generation,
                LoopWorkerRequest.kind == "patrol_evidence",
                LoopWorkerRequest.scope["patrol_session_id"].astext == session_id).limit(1)) is not None

    async def missing(self, observation, needs):
        try:
            required = source_revisions(observation, needs.context_ids)
        except ValueError as exc:
            raise PatrolContractViolation(str(exc)) from exc
        async with self._sessions() as session:
            results = await self._supplements.curator_results(session, observation)
            available = {key for item in results for key in item["required_source_revisions"]}
            missing = []
            if "curator" in needs.needs_evidence and required.keys() - available:
                missing.append("curator")
            if "expansion_assessment" in needs.needs_evidence and await self._supplements.assessment(
                    session, observation, needs.context_ids) is None:
                missing.append("expansion_assessment")
            return tuple(missing)

    async def recover_request(self, observation):
        # A crash after the model's durable receipt but before enqueue must not
        # lose its request or repeat an already charged cognitive call.
        async with self._sessions() as session:
            attempt = await session.scalar(select(LoopPatrolAttempt).where(
                LoopPatrolAttempt.observation_id == (observation.observation_id or observation.decision_inputs_ref))
                .order_by(LoopPatrolAttempt.attempt.desc()).limit(1))
            needs = PatrolEvidenceNeeds.model_validate({"needs_evidence": attempt.raw_output["needs_evidence"], "context_ids": attempt.raw_output.get("context_ids", ())}) \
                if attempt is not None and attempt.status == "waiting_evidence" else None
        if needs is not None:
            try:
                if await self.missing(observation, needs):
                    return needs
            except PatrolContractViolation:
                # A crash between receipt persistence and scope admission must
                # not turn invalid model output into an endlessly replayed job.
                return None
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
            curator = await self._supplements.curator_results(session, observation)
        if request.scope["stage"] == "prepare":
            await self._curators.dispatch(request.scope["patrol_session_id"], observation,
                self._curators.scopes(observation, request.scope.get("context_ids", ())), worker_request=request)
        elif request.scope["stage"] == "assessment":
            required = source_revisions(observation, request.scope.get("context_ids", ()))
            curator = tuple(item for item in curator if set(item["required_source_revisions"]).issubset(required))
            if required.keys() - {key for item in curator for key in item["required_source_revisions"]}:
                raise ValueError("派生评估缺少本 Observation 的 Curator 证据")
            observation = PatrolDecisionContext(observation, curator_results=curator).model_observation()
            assessment = await self._expansions.assess(observation)
            return {"stage": "assessment", "observation_id": request.observation_id,
                    "assessment": assessment.model_dump(mode="json")}
        else:
            raise ValueError("未知 Patrol evidence stage")
        return {"stage": request.scope["stage"], "observation_id": request.observation_id}
