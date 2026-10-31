"""Durable scoped evidence, attempt fencing and commit-driven Round wakeup."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.patrol.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.patrol.agent_loop.decisions.decision_context import DecisionSupplementRepository, PatrolDecisionContext
from backend.app.desktop.patrol.agent_loop.decisions.evidence import PatrolEvidenceNeeds
from backend.app.desktop.patrol.agent_loop.decisions.evidence_jobs import EvidenceJob, EvidenceJobRepository, source_revisions
from backend.app.desktop.patrol.agent_loop.models import LoopEventOutbox, LoopRound, LoopWorkerRequest
from backend.app.desktop.patrol.agent_loop.observations.builder import observation_hash
from backend.app.desktop.patrol.agent_loop.rounds.round_orchestration import LoopRoundOrchestrator, PatrolCognitiveStep
from backend.app.desktop.patrol.agent_loop.sessions.patrol_runtime import CuratorCoordinationStage
from backend.app.desktop.patrol.agent_loop.task_progress.contracts import canonical_hash
from backend.app.desktop.patrol.agent_loop.workers.runtime import LoopWorkerRuntime
from backend.app.desktop.patrol.agent_loop.workers.worker_attempts import WorkerAttemptAuthority, WorkerAttemptRejected
from backend.app.desktop.persistence.state_changes import PostgresNotifications
from backend.tests.test_patrol_evidence_demand import _observation
from backend.tests.test_patrol_session import _create_loop, _persist_observation
from backend.tests.test_patrol_session import _observation_envelope


def _result(observation):
    payload = {"work_specs": []}
    return {"round_id": observation.round_id, "kind": "lane_curator", "result": payload,
        "result_hash": canonical_hash(payload), "observation_id": observation.observation_id,
        "observation_generation": observation.generation,
        "required_source_revisions": source_revisions(observation)}


@pytest.mark.parametrize("change", ["observation_id", "observation_generation", "required_source_revisions", "result_hash", "round_id"])
def test_decision_rejects_late_or_changed_identity(change):
    observation = _observation(observation_id="observation", decision_inputs_ref="observation")
    valid = _result(observation)
    invalid = deepcopy(valid)
    invalid[change] = {} if change == "required_source_revisions" else "stale"
    composed = PatrolDecisionContext(observation, curator_results=(invalid, valid)).model_observation()
    assert composed.worker_results == (valid,)
    assert observation.worker_results == ()


def test_composition_retains_frozen_worker_facts_and_deduplicates_exact_results():
    observation = _observation(observation_id="observation", decision_inputs_ref="observation",
        worker_results=({"request_id": "verifier", "kind": "completion_verifier", "result": {"passed": True}},))
    valid = _result(observation)
    composed = PatrolDecisionContext(observation, curator_results=(valid, valid)).model_observation()
    assert composed.worker_results == (*observation.worker_results, valid)
    assert PatrolDecisionContext(observation, curator_results=()).model_observation() == observation


def test_unknown_model_context_is_a_retryable_contract_violation():
    from backend.app.desktop.patrol.agent_loop.decisions.evidence_queue import PatrolEvidenceQueue
    from backend.app.desktop.patrol.agent_loop.patrol import PatrolContractViolation

    queue = PatrolEvidenceQueue(None, None, None, None)
    with pytest.raises(PatrolContractViolation, match="evidence_source_unknown"):
        asyncio.run(queue.missing(_observation(), PatrolEvidenceNeeds(needs_evidence=("curator",), context_ids=("typo",))))


def test_evidence_request_preserves_scope_and_does_not_truncate_contexts():
    request = PatrolCognitiveStep(needs_evidence=("curator",), context_ids=("context",))
    assert request.context_ids == ("context",)
    observation = _observation(portfolio_frontier=tuple(
        {"context_id": str(i), "revision_id": f"r{i}"} for i in range(12)))
    stage = CuratorCoordinationStage(None, max_assignments=2)
    assert len(stage.scopes(observation)) == 12
    assert [s["context_id"] for s in stage.scopes(observation, ("11",))] == ["11"]
    with pytest.raises(ValueError, match="evidence_source_unknown"):
        stage.scopes(observation, ("unknown",))


def test_planned_checkpoint_cannot_be_reused_with_different_source_revisions():
    from backend.app.desktop.patrol.agent_loop.context_expansion.retrieval_planner import RetrievalBackedCognitiveAdvisor

    source = {"context_id": "context", "revision_id": "old"}
    index = SimpleNamespace(source=SimpleNamespace(context_id="context", model_dump=lambda **_: source), index_id="index")
    planning = SimpleNamespace(state="planned", catalog_id="catalog", frontier_hash="frontier", authorized_index_ids=("index",))
    payload = {"frontier_hash": "frontier", "scope": {"derivation_input": {
        "portfolio_index_catalog": {"catalog_id": "catalog"},
        "required_source_revisions": {"context": {**source, "revision_id": "new"}}}}}
    with pytest.raises(ValueError, match="retrieval_evidence_identity_mismatch"):
        asyncio.run(RetrievalBackedCognitiveAdvisor(None).plan(payload, planning, (index,)))


@pytest.mark.usefixtures("isolated_postgres_database")
def test_completed_index_survives_a_sibling_failure_and_is_immediately_readable(tmp_path):
    from backend.app.desktop.patrol.agent_loop.context_expansion.models import LoopSemanticIndexArtifact
    from backend.tests.incremental_index_support import ModelProbe, messages, observation, revision
    from backend.tests.test_incremental_revision_index import exercise

    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions)
        target = await revision(sessions, seed["context_id"], messages(2))
        frozen = observation(seed, target)
        frozen.portfolio_frontier += ({"context_id": "unrelated", "revision_id": "missing",
                                       "revision": {"revision_id": "missing"}},)
        bounded = service._bounded_index
        published, fail = asyncio.Event(), asyncio.Event()
        build_and_publish = service._build_and_publish

        async def build_one(obs, item, semaphore):
            if item.get("context_id") == "unrelated":
                await fail.wait()
                raise ValueError("unrelated_context_failed")
            return await bounded(obs, item, semaphore)

        async def save(obs, item, semaphore):
            result = await build_and_publish(obs, item, semaphore)
            published.set()
            return result

        service._bounded_index, service._build_and_publish = build_one, save
        task = asyncio.create_task(service.build(frozen))
        try:
            await asyncio.wait_for(published.wait(), 5)
            async with sessions() as session:
                artifact = await session.scalar(select(LoopSemanticIndexArtifact).where(
                    LoopSemanticIndexArtifact.revision_id == target.ref.revision_id))
            assert artifact is not None and not task.done()
            fail.set()
            result = await task
            assert result.blocker_code == "portfolio_index_failed"
            async with sessions() as session:
                assert await session.get(LoopSemanticIndexArtifact, artifact.index_id) is not None
        finally:
            fail.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    exercise(tmp_path, run)


@pytest.mark.usefixtures("isolated_postgres_database")
def test_context_result_is_durable_and_wakes_without_unrelated_workers(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot = await _create_loop(sessions, tmp_path)
        coordinator = LoopCoordinator(sessions)
        runtime = LoopWorkerRuntime(sessions, object())
        orchestrator = LoopRoundOrchestrator(sessions, object(), None, None)
        builds = []

        class Index:
            async def build(self, observation, *, required_context_ids, worker_request):
                builds.append(required_context_ids)
                assert worker_request.retry_identity
                return SimpleNamespace(catalog=SimpleNamespace(model_dump=lambda **_: {"catalog_id": "a" * 64}),
                                       indexes=(), stage_record=None)

        orchestrator._curators = CuratorCoordinationStage(sessions, index_service=Index())
        runtime.bind_evidence_handler(orchestrator.prepare_evidence)
        ready, arrived = asyncio.Event(), asyncio.Event()
        hints = []

        def notify(payload):
            hints.append(json.loads(payload))
            arrived.set()

        feed = PostgresNotifications(sessions, {"focus_loop_work": notify}, ready.set,
                                     application_name="focus-evidence-test")
        try:
            claim = await coordinator.claim_for_loop(snapshot["loop_id"], "evidence")
            observation = _observation_envelope(claim.loop_id, claim.round_id).model_copy(update={
                "portfolio_frontier": tuple({"context_id": str(i), "revision_id": f"r{i}"} for i in range(3))})
            observation = await _persist_observation(sessions, observation)
            digest = observation_hash(observation)
            patrol = await orchestrator._patrol_sessions.begin(claim)
            class Frozen:
                async def capture(self, *_):
                    return observation
            orchestrator._observations = Frozen()
            await orchestrator._prepare_observation(claim, patrol.session_id, "observed")
            queue = orchestrator._evidence_queue()
            needs = PatrolEvidenceNeeds(needs_evidence=("curator",), context_ids=("0",))
            first = await queue.enqueue(claim, patrol.session_id, observation, needs)
            assert await queue.enqueue(claim, patrol.session_id, observation, needs) == first
            await coordinator.release(claim)
            assert await coordinator.claim_for_loop(claim.loop_id, "while-waiting") is None
            preparation, = await runtime._claim_many(1, claim.loop_id)
            await runtime._run_one(preparation)
            assert builds == [("0",)]
            curator, = await runtime._claim_many(1, claim.loop_id)
            assert curator.kind == "lane_curator"
            # Reclaiming a running attempt increments the job fence. The old
            # callback cannot publish even when the model result is identical.
            stale = deepcopy(curator)
            async with sessions.begin() as session:
                worker = await session.get(LoopWorkerRequest, curator.worker_request_id)
                await EvidenceJobRepository().claim(session, worker)
                curator = deepcopy(worker)
            with pytest.raises(ValueError, match="evidence_fencing_rejected"):
                async with sessions.begin() as session:
                    await EvidenceJobRepository().finish(session, stale, "ready", {"work_specs": []})

            async with sessions.begin() as session:
                # An unrelated Worker must never form a barrier for Context 0.
                session.add(LoopWorkerRequest(worker_request_id=uuid.uuid4().hex, loop_id=claim.loop_id,
                    round_id=claim.round_id, observation_id=observation.observation_id,
                    observation_generation=observation.generation, kind="completion_verifier", scope={}))
            feed.start()
            await asyncio.wait_for(ready.wait(), 5)
            hints.clear()
            arrived.clear()
            async with sessions.begin() as session:
                _, worker = await WorkerAttemptAuthority().require_current(session, curator)
                await EvidenceJobRepository().finish(session, worker, "ready", {"work_specs": []})
                # NOTIFY and durable result are invisible until commit.
                await asyncio.sleep(0.03)
                assert not hints
                async with sessions() as reader:
                    assert await DecisionSupplementRepository().curator_results(reader, observation) == ()
                worker.status, worker.result, worker.completed_at = "success", {"work_specs": []}, datetime.now(UTC)
            await asyncio.wait_for(arrived.wait(), 2)
            assert any("rounds" in hint["components"] for hint in hints)
            # Simulate a crash before runtime._advance: job completion alone
            # made the Round claimable in the same transaction.
            resumed = await coordinator.claim_for_loop(claim.loop_id, "restarted")
            assert resumed is not None
            async with sessions() as session:
                results = await DecisionSupplementRepository().curator_results(session, observation)
                assert len(results) == 1 and results[0]["required_source_revisions"] == source_revisions(observation, ("0",))
                assert await session.scalar(select(func.count()).select_from(EvidenceJob).where(EvidenceJob.round_id == claim.round_id)) == 2
                assert await session.scalar(select(func.count()).select_from(LoopEventOutbox).where(
                    LoopEventOutbox.loop_id == claim.loop_id, LoopEventOutbox.event_type == "EvidenceJobReady")) == 2
            assert await queue.missing(observation, needs) == ()
            assert await queue.missing(observation, PatrolEvidenceNeeds(needs_evidence=("curator",), context_ids=("1",))) == ("curator",)
            assert observation_hash(observation) == digest
            # Completed results cannot be reopened or replaced by a retry.
            with pytest.raises(ValueError, match="evidence_result_immutable"):
                async with sessions.begin() as session:
                    worker = await session.get(LoopWorkerRequest, curator.worker_request_id)
                    await EvidenceJobRepository().claim(session, worker)
            # A replaced Observation also revokes a still-claimed attempt.
            async with sessions.begin() as session:
                (await session.get(LoopRound, claim.round_id)).observation_id = None
            with pytest.raises(WorkerAttemptRejected):
                async with sessions.begin() as session:
                    await EvidenceJobRepository().finish(session, curator, "ready", {"work_specs": []})
        finally:
            await feed.close()
            await runtime.close()
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_preupgrade_curator_is_adopted_and_control_updates_task_state(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot = await _create_loop(sessions, tmp_path)
        runtime = LoopWorkerRuntime(sessions, object())
        orchestrator = LoopRoundOrchestrator(sessions, object(), None, None)

        class Index:
            async def build(self, observation, **_):
                return SimpleNamespace(catalog=SimpleNamespace(model_dump=lambda **_: {"catalog_id": "a" * 64}),
                                       indexes=(), stage_record=None)

        orchestrator._curators = CuratorCoordinationStage(sessions, index_service=Index())
        runtime.bind_evidence_handler(orchestrator.prepare_evidence)
        try:
            claim = await LoopCoordinator(sessions).claim_for_loop(snapshot["loop_id"], "legacy")
            frozen = await _persist_observation(sessions, _observation_envelope(claim.loop_id, claim.round_id).model_copy(update={
                "portfolio_frontier": ({"context_id": "context", "revision_id": "revision"},)}))
            class Frozen:
                async def capture(self, *_):
                    return frozen
            orchestrator._observations = Frozen()
            patrol = await orchestrator._patrol_sessions.begin(claim)
            await orchestrator._prepare_observation(claim, patrol.session_id, "observed")
            await orchestrator._evidence_queue().enqueue(claim, patrol.session_id, frozen,
                PatrolEvidenceNeeds(needs_evidence=("curator",), context_ids=("context",)))
            preparation, = await runtime._claim_many(1, claim.loop_id)
            await runtime._run_one(preparation)
            curator, = await runtime._claim_many(1, claim.loop_id)
            async with sessions.begin() as session:
                # Simulate pre-upgrade durable rows: bound Worker/assignment,
                # no EvidenceJob or job metadata, and a successful result.
                await session.execute(delete(EvidenceJob).where(EvidenceJob.worker_request_id == curator.worker_request_id))
                worker = await session.get(LoopWorkerRequest, curator.worker_request_id)
                worker.scope = {key: value for key, value in worker.scope.items() if key not in {"evidence_job_id", "evidence_fencing_token"}}
                worker.status, worker.result, worker.completed_at = "success", {"work_specs": []}, datetime.now(UTC)
                (await session.get(LoopRound, claim.round_id)).status = "curated"
            resumed = await orchestrator._prepare_observation(claim, patrol.session_id, "curated")
            assert len(resumed.worker_results) == 1
            assert resumed.worker_results[0]["request_id"] == curator.worker_request_id
            async with sessions() as session:
                job = await session.scalar(select(EvidenceJob).where(EvidenceJob.worker_request_id == curator.worker_request_id))
                assert job.status == "ready" and job.result_hash == canonical_hash({"work_specs": []})
            # Existing bulk recovery/control transitions synchronize unfinished
            # jobs transactionally, without reopening completed evidence.
            worker_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(LoopWorkerRequest(worker_request_id=worker_id, loop_id=claim.loop_id,
                    round_id=claim.round_id, observation_id=frozen.observation_id,
                    observation_generation=frozen.generation, kind="patrol_evidence", scope={}))
                await session.flush()
                pending = await EvidenceJobRepository().ensure(session, frozen, "context_index_retry", ("context",), worker_id)
                job_id = pending.job_id
            for status, expected in (("running", "running"), ("pending", "pending"), ("error", "failed"), ("pending", "pending"), ("cancelled", "cancelled")):
                async with sessions.begin() as session:
                    await session.execute(update(LoopWorkerRequest).where(LoopWorkerRequest.worker_request_id == worker_id).values(status=status))
                async with sessions() as session:
                    assert (await session.get(EvidenceJob, job_id)).status == expected
            async with sessions.begin() as session:
                await session.execute(update(LoopWorkerRequest).where(LoopWorkerRequest.worker_request_id == curator.worker_request_id).values(status="cancelled"))
            async with sessions() as session:
                assert (await session.get(EvidenceJob, job.job_id)).status == "ready"
        finally:
            await runtime.close()
            await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())
