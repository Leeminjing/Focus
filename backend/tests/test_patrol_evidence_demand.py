"""Demand-driven Patrol evidence: admission, durable wait and frozen resumption."""

import asyncio
from datetime import UTC, datetime
import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.patrol.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.patrol.agent_loop.context_expansion.contracts import ExpansionAssessment
from backend.app.desktop.patrol.agent_loop.decisions.decision_context import PatrolDecisionContext
from backend.app.desktop.patrol.agent_loop.decisions.evidence import PatrolEvidenceNeeds, PatrolEvidencePolicy
from backend.app.desktop.patrol.agent_loop.kernel import LoopKernel
from backend.app.desktop.patrol.agent_loop.models import LoopDecision, LoopObservation, LoopPatrolAttempt, LoopRound, LoopWorkerRequest
from backend.app.desktop.patrol.agent_loop.observations.builder import observation_hash
from backend.app.desktop.patrol.agent_loop.patrol import PatrolContractViolation
from backend.app.desktop.patrol.agent_loop.patrol_contract import PatrolDecisionContract
from backend.app.desktop.patrol.agent_loop.rounds.round_orchestration import LoopRoundOrchestrator, PatrolCognitiveStep, StructuredPatrolDecisionModel
from backend.app.desktop.patrol.agent_loop.schemas import AdoptWorkspaceResultAction, ContinueContextAction, ContextPermission, LoopObservationEnvelope, PatrolDecisionIntent
from backend.app.desktop.patrol.agent_loop.sessions.patrol_runtime import CuratorCoordinationStage
from backend.app.desktop.patrol.agent_loop.workers.runtime import LoopWorkerRuntime
from backend.app.desktop.patrol.agent_loop.workers.worker_attempts import WorkerAttemptAuthority, WorkerAttemptRejected
from backend.tests.config_helpers import app_config_for
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_round_task_progress import _Checkpointer


def _observation(**update):
    return LoopObservationEnvelope(loop_id="loop", round_id="round", loop_revision=1,
        goal_revision=1, authority_revision=1, observed_frontier_hash="a" * 64,
        mission={"outcome": "deliver"}, grant={"capabilities": ["continue_context", "adopt_workspace_result"], "context_scope": ["context"]},
        portfolio_frontier=({"context_id": "context", "revision_id": "revision"},),
        workspace={"revision": 1}, budget={}).model_copy(update=update)


def _continue():
    return ContinueContextAction(action="continue_context", context_id="context",
        context_revision_id="revision", message="continue")


@pytest.mark.parametrize("other", [{"decision": {}}, {"reads": [{"context_id": "c", "revision_id": "r"}]},
    {"needs_evidence": ["curator", "curator"]}, {"needs_evidence": ["permission"]}])
def test_evidence_step_rejects_mixed_or_unknown_output(other):
    with pytest.raises(ValidationError):
        PatrolCognitiveStep.model_validate({"needs_evidence": ["curator"], **other})


def test_evidence_step_works_without_a_selective_reader():
    model = StructuredPatrolDecisionModel(object())
    async def invoke(*args):
        return PatrolCognitiveStep(needs_evidence=("expansion_assessment",))
    model._invoke = invoke
    result = asyncio.run(model._decide(None, [], "prompt_json", _observation()))
    assert result.needs_evidence == ("expansion_assessment",)


def test_read_limit_still_allows_a_durable_evidence_request():
    read = PatrolCognitiveStep.model_validate({"reads": [{"context_id": "context", "revision_id": "revision"}]})
    steps = iter((read, read, read, PatrolCognitiveStep(needs_evidence=("curator",))))
    async def invoke(*args):
        return next(steps)
    async def reader(*args):
        return ()
    model = StructuredPatrolDecisionModel(object(), reader=reader)
    model._invoke = invoke
    result = asyncio.run(model._decide(None, [], "prompt_json", _observation()))
    assert result.needs_evidence == ("curator",)


def test_continue_does_not_need_derivation_but_cannot_skip_known_required_work():
    action = _continue()
    observation = _observation()
    assert PatrolEvidencePolicy.missing((action,), observation) == ()
    PatrolEvidencePolicy.validate((action,), observation)
    for update in ({"worker_results": ({"round_id": "round", "kind": "lane_curator", "result": {"work_specs": [{"required": True}]}},)},
                   {"expansion_obligations": ({"opportunity_id": "required"},)}):
        assert PatrolEvidencePolicy.missing((action,), observation.model_copy(update=update)) == ("expansion_assessment",)
    historical = {"round_id": "previous", "kind": "lane_curator", "result": {"work_specs": [{"required": True}]}}
    assert PatrolEvidencePolicy.missing((action,), observation.model_copy(update={"worker_results": (historical,)})) == ()
    required = observation.model_copy(update={"expansion_assessment": {"level": "required"}})
    with pytest.raises(PatrolContractViolation, match="required"):
        PatrolDecisionContract().validate(actions=(action,), mission_references=(), observation=required)
    # A finished assessment cannot silently erase an existing unresolved obligation.
    with pytest.raises(PatrolContractViolation, match="required"):
        PatrolDecisionContract().validate(actions=(action,), mission_references=(), observation=observation.model_copy(
            update={"expansion_obligations": ({"opportunity_id": "required"},),
                    "expansion_assessment": {"level": "not_applicable"}}))


@pytest.mark.parametrize("update", [{"grant": {}}, {"portfolio_frontier": ()},
    {"portfolio_frontier": ({"context_id": "context", "revision_id": "new"},)},
    {"stable_results": ({"context_id": "context", "run_id": "active", "status": "running"},)}])
def test_continue_requires_exact_revision_authority_and_no_run_conflict(update):
    with pytest.raises(PatrolContractViolation):
        PatrolEvidencePolicy.validate((_continue(),), _observation(**update))


def test_adoption_requires_exact_version_conflicts_and_independent_verification():
    action = AdoptWorkspaceResultAction(action="adopt_workspace_result", source_slot_id="slot", source_revision=2, rationale="verified")
    slot = {"slot_id": "slot", "revision": 2}
    with pytest.raises(PatrolContractViolation, match="独立"):
        PatrolEvidencePolicy.validate((action,), _observation(workspace={"revision": 1, "isolated_slots": [slot]}))
    ready = {**slot, "adoption_admission": {"status": "ready", "target_revision": 1}}
    PatrolEvidencePolicy.validate((action,), _observation(workspace={"revision": 1, "isolated_slots": [ready]}))
    with pytest.raises(PatrolContractViolation, match="目标版本"):
        PatrolEvidencePolicy.validate((action,), _observation(workspace={"revision": 3, "isolated_slots": [ready]}))


def test_merge_requires_exact_sources_and_complete_committed_lineage():
    from backend.app.desktop.patrol.agent_loop.task_progress.contracts import canonical_hash

    frontier = tuple({"context_id": str(i), "revision_id": f"r{i}", "checkpoint_id": f"c{i}"} for i in (1, 2))
    refs = tuple(SimpleNamespace(context_id=item["context_id"], revision_id=item["revision_id"],
        checkpoint_id=item["checkpoint_id"]) for item in frontier)
    merge = SimpleNamespace(action="merge_contexts", plan=SimpleNamespace(source_frontier=refs))
    lineage = {"roots": {str(i): f"r{i}" for i in (1, 2)}, "nodes": [
        {"context_id": str(i), "revision_id": f"r{i}", "generation": 1} for i in (1, 2)],
        "edges": [], "topology_hash": canonical_hash([])}
    observation = _observation(portfolio_frontier=frontier, committed_lineage=lineage,
        grant={"capabilities": ["merge_contexts"], "context_scope": ["1", "2"]})
    PatrolEvidencePolicy.validate((merge,), observation)
    with pytest.raises(PatrolContractViolation, match="Lineage"):
        PatrolEvidencePolicy.validate((merge,), observation.model_copy(update={"committed_lineage": None}))
    with pytest.raises(PatrolContractViolation, match="Revision"):
        PatrolEvidencePolicy.validate((merge,), observation.model_copy(update={"portfolio_frontier": (
            {**frontier[0], "revision_id": "stale"}, frontier[1])}))


def test_completion_reuses_existing_eligibility_contract():
    from backend.app.desktop.patrol.agent_loop.schemas import RequestCompletionAction
    from backend.app.desktop.patrol.agent_loop.completion.completion_eligibility import CompletionEligibilityPolicy

    action = RequestCompletionAction(action="request_completion", verification_id="v", final_context_ids=("context",), final_slot_id="slot")
    with pytest.raises(PatrolContractViolation, match="资格证据"):
        PatrolEvidencePolicy.validate((action,), _observation())
    eligibility = {"contract": CompletionEligibilityPolicy.VERSION, "blockers": [], "final_slot_id": "slot",
        "final_context_ids": ["context"], "verifications": [{"verification_id": "v", "allowed": True, "reasons": []}]}
    PatrolEvidencePolicy.validate((action,), _observation(completion_eligibility=eligibility))
    with pytest.raises(PatrolContractViolation, match="workspace_not_adopted"):
        PatrolEvidencePolicy.validate((action,), _observation(completion_eligibility={**eligibility, "blockers": ["workspace_not_adopted"]}))


def test_workspace_verification_requires_an_independent_context_and_exact_version():
    from backend.app.desktop.patrol.agent_loop.decisions.evidence import workspace_admission_facts

    writer = SimpleNamespace(run_id="write", task_id="producer", status="success", settled_at=True, workspace_anchor={"mode": "write"})
    reviewer = SimpleNamespace(run_id="review", task_id="reviewer", status="success", settled_at=True, workspace_anchor={"mode": "read"})
    anchor = SimpleNamespace(slot_id="slot", settled_at=True, resulting_fingerprint="hash",
        resulting_workspace_revision=2, observed_workspace_revision=2, observed_fingerprint="hash")
    slot = SimpleNamespace(slot_id="slot", revision=2, base_revision="baseline", current_fingerprint="hash")
    class Session:
        async def execute(self, _):
            return SimpleNamespace(all=lambda: [(writer, anchor), (reviewer, anchor)])
    async def facts():
        return (await workspace_admission_facts(Session(), "loop", [slot], SimpleNamespace(revision=3), []))["slot"]
    assert asyncio.run(facts())["status"] == "ready"
    reviewer.task_id = "producer"
    assert asyncio.run(facts())["status"] == "blocked"
    reviewer.task_id = "reviewer"
    anchor.observed_workspace_revision = 1
    assert asyncio.run(facts())["status"] == "blocked"


def _continuation(observation):
    import uuid

    source = observation.portfolio_frontier[0]
    return PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key=uuid.uuid4().hex,
        loop_id=observation.loop_id, round_id=observation.round_id, loop_revision=observation.loop_revision,
        observation_id=observation.observation_id, observation_generation=observation.generation,
        holder_id=observation.grant["holder_id"], grant_id=observation.grant["grant_id"],
        grant_revision=observation.authority_revision, goal_revision=observation.goal_revision,
        observed_frontier_hash=observation.observed_frontier_hash,
        observed_workspace_revision=observation.workspace["revision"],
        observed_projection_sequence=observation.projection_sequence, base_entity_revisions=observation.base_entity_revisions,
        rationale="continue with frozen facts", context_permissions=tuple(ContextPermission(
            context_id=item["context_id"], access_mode="read-only") for item in observation.portfolio_frontier),
        actions=(ContinueContextAction(action="continue_context", context_id=source["context_id"],
            context_revision_id=source["revision_id"], message="continue"),))


@pytest.mark.usefixtures("isolated_postgres_database")
@pytest.mark.parametrize("request_evidence", [False, True])
def test_process_commits_or_persists_evidence_demand_without_eager_work(tmp_path, request_evidence):
    from focus.runtime.runs.usage import ModelUsage

    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="fast-patrol", started_at=datetime.now(UTC))
        coordinator = LoopCoordinator(sessions)
        orchestrator = LoopRoundOrchestrator(sessions, object(), LoopKernel(sessions, require_fencing=True), _Checkpointer())
        class Model:
            usage = ModelUsage()
            async def __call__(self, observation):
                assert observation.expansion_assessment is None
                if request_evidence:
                    return PatrolEvidenceNeeds(needs_evidence=("curator",))
                return _continuation(observation)
        async def forbidden(*args, **kwargs):
            raise AssertionError("Unrequested evidence must not run")
        orchestrator._decision_model = lambda _: Model()
        orchestrator._curators.dispatch = forbidden
        orchestrator._expansions.assess = forbidden
        try:
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "fast-path")
            result = await orchestrator.process(claim)
            if request_evidence:
                assert result is None
            else:
                assert result.status == "committed", result.reason
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopWorkerRequest).where(LoopWorkerRequest.round_id == claim.round_id)) == int(request_evidence)
                assert await session.scalar(select(func.count()).select_from(LoopDecision).where(LoopDecision.round_id == claim.round_id)) == int(not request_evidence)
                if request_evidence:
                    attempt = await session.scalar(select(LoopPatrolAttempt).where(LoopPatrolAttempt.round_id == claim.round_id))
                    assert attempt.status == "waiting_evidence"
                    assert attempt.raw_output["needs_evidence"] == ["curator"]
            if request_evidence:
                runtime = LoopWorkerRuntime(sessions, object())
                job, = await runtime._claim_many(1, fixture["loop_id"])
                # A replaced Observation revokes even a still-claimed job.
                async with sessions.begin() as session:
                    (await session.get(LoopRound, claim.round_id)).observation_id = None
                with pytest.raises(WorkerAttemptRejected):
                    await orchestrator.prepare_evidence(job)
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_evidence_queue_resumes_same_observation_and_survives_orchestrator_restart(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="evidence", started_at=datetime.now(UTC))
        coordinator = LoopCoordinator(sessions)
        runtime = LoopWorkerRuntime(sessions, object())
        orchestrator = LoopRoundOrchestrator(sessions, object(), None, _Checkpointer())
        class Index:
            async def build(self, observation, **kwargs):
                return SimpleNamespace(catalog=SimpleNamespace(model_dump=lambda **kwargs: {"catalog_id": "a" * 64}), indexes=(), stage_record=None)
        orchestrator._curators = CuratorCoordinationStage(sessions, index_service=Index())
        try:
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "evidence-test")
            frozen = await orchestrator._observations.capture(claim.loop_id, claim.round_id)
            patrol = await orchestrator._patrol_sessions.begin(claim)
            immediate = await orchestrator._prepare_observation(claim, patrol.session_id, "observed")
            assert immediate is not None and immediate.expansion_assessment is None
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopWorkerRequest).where(LoopWorkerRequest.round_id == claim.round_id)) == 0
            digest = observation_hash(frozen)
            needs = PatrolEvidenceNeeds(needs_evidence=("expansion_assessment",))
            queue = orchestrator._evidence_queue()
            # Persist the cognitive receipt, then simulate losing the process
            # before its enqueue transaction. Recovery consumes this receipt.
            import uuid
            async with sessions.begin() as session:
                session.add(LoopPatrolAttempt(patrol_attempt_id=uuid.uuid4().hex,
                    loop_id=claim.loop_id, round_id=claim.round_id, observation_id=frozen.decision_inputs_ref,
                    attempt=1, execution_thread_id="evidence-attempt", checkpoint_ns="patrol",
                    observation_hash=digest, status="waiting_evidence", raw_output=needs.model_dump(mode="json")))
            assert await queue.recover_request(frozen) == needs
            first = await queue.enqueue(claim, patrol.session_id, frozen, needs)
            assert await queue.enqueue(claim, patrol.session_id, frozen, needs) == first
            async with sessions() as session:
                assert (await session.get(LoopRound, claim.round_id)).status == "waiting_workers"
                assert await session.scalar(select(func.count()).select_from(LoopDecision).where(LoopDecision.round_id == claim.round_id)) == 0
            # A fresh coordinator, queue and handler read only durable identities.
            await coordinator.release(claim)
            assert await coordinator.claim_for_loop(fixture["loop_id"], "waiting") is None
            runtime.bind_evidence_handler(orchestrator.prepare_evidence)
            preparation, = await runtime._claim_many(1, fixture["loop_id"])
            async def stopped(_):
                raise asyncio.CancelledError()
            runtime.bind_evidence_handler(stopped)
            with pytest.raises(asyncio.CancelledError):
                await runtime._run_one(preparation)
            async with sessions() as session:
                interrupted = await session.get(LoopWorkerRequest, preparation.worker_request_id)
                assert interrupted.status == "pending" and interrupted.attempt == 2
            runtime = LoopWorkerRuntime(sessions, object())
            runtime.bind_evidence_handler(orchestrator.prepare_evidence)
            preparation, = await runtime._claim_many(1, fixture["loop_id"])
            await runtime._run_one(preparation)
            curator, = await runtime._claim_many(1, fixture["loop_id"])
            await runtime._start_curator_analysis(curator)
            async with sessions.begin() as session:
                _, row = await WorkerAttemptAuthority().require_current(session, curator)
                assignment = await runtime._curator_assignments.by_worker(session, curator.worker_request_id, lock=True)
                await runtime._curator_assignments.transition(session, assignment.assignment_id, "proposed", "ready")
                row.status, row.result, row.completed_at = "success", {"work_specs": []}, datetime.now(UTC)
            await runtime._advance(curator.loop_id, curator.round_id, request=curator)
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "resumed")
            assert claim is not None
            await orchestrator._patrol_sessions.begin(claim)
            resumed = await orchestrator._prepare_observation(claim, patrol.session_id, "curated")
            assert resumed.decision_inputs_ref == frozen.decision_inputs_ref
            assert await queue.assessment_requested(resumed)
            await queue.enqueue(claim, patrol.session_id, resumed, needs)
            async def assess(observation, **kwargs):
                assert observation.decision_inputs_ref == frozen.decision_inputs_ref
                assert len(observation.worker_results) == 1
                return ExpansionAssessment(loop_id=observation.loop_id, round_id=observation.round_id,
                    frontier_hash=observation.observed_frontier_hash, policy_version="test", level="not_applicable")
            orchestrator._expansions.assess = assess
            assessment, = await runtime._claim_many(1, fixture["loop_id"])
            await runtime._run_one(assessment)
            composed = await orchestrator._prepare_observation(claim, patrol.session_id, "curated")
            async with sessions() as session:
                saved = await queue._supplements.get(session, frozen.decision_inputs_ref, "expansion_assessment")
                base = await session.get(LoopObservation, frozen.decision_inputs_ref)
                assert base.envelope_hash == digest
                assert observation_hash(LoopObservationEnvelope.model_validate(base.envelope)) == digest
                assert base.envelope["expansion_assessment"] is None
                assert await session.scalar(select(func.count()).select_from(LoopObservation).where(LoopObservation.loop_id == fixture["loop_id"])) == 1
            composed = PatrolDecisionContext(composed, expansion_assessment=saved).model_observation()
            assert composed.expansion_assessment["level"] == "not_applicable"
            assert await queue.missing(composed, needs) == ()
            assert await queue.recover_request(composed) is None
            with pytest.raises(PatrolContractViolation, match="已经可用"):
                await queue.enqueue(claim, patrol.session_id, composed, needs)
            from focus.runtime.runs.usage import ModelUsage

            class DecisionModel:
                usage = ModelUsage()
                async def __call__(self, observation):
                    assert observation.decision_inputs_ref == frozen.decision_inputs_ref
                    assert observation.expansion_assessment["level"] == "not_applicable"
                    return _continuation(observation)
            orchestrator._decision_model = lambda _: DecisionModel()
            orchestrator._kernel = LoopKernel(sessions, require_fencing=True)
            result = await orchestrator.process(claim)
            assert result.status == "committed", result.reason
        finally:
            await runtime.close()
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
