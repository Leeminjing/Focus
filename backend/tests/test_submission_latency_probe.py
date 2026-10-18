"""Real PostgreSQL checks for causal and publication gates in latency probes."""

import asyncio
from datetime import UTC, datetime
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.models import LoopAction, LoopDecision, LoopDirective, LoopObservation, LoopRound
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.submission_latency import CommittedSubmissionProbe
from backend.app.desktop.context_evolution.models import ContextPublicationReceipt
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.run_orchestration.models import RunDispatch
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@pytest.mark.parametrize("action_type", ["create_lane", "continue_context"])
def test_probe_ignores_unrelated_decision_uncommitted_authority_and_unpublished_context(tmp_path, action_type):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        seed = await _seed_loop(sessions, tmp_path, label="latency", started_at=datetime.now(UTC))
        probe = CommittedSubmissionProbe(sessions)
        receipt = {"loop_id": seed["loop_id"], "intent_id": "wanted", "round_id": seed["round_id"]}
        observation_id, decision_id = uuid.uuid4().hex, uuid.uuid4().hex
        try:
            async with sessions.begin() as session:
                session.add(LoopObservation(observation_id=observation_id, loop_id=seed["loop_id"],
                    round_id=seed["round_id"], generation=100,
                    envelope={"causal_chain": [{"intent_id": "different"}]}, envelope_hash="a" * 64))
                await session.flush()
                session.add(LoopDecision(decision_id=decision_id, loop_id=seed["loop_id"], round_id=seed["round_id"],
                    observation_id=observation_id, holder_id="patrol", intent={}, rationale="test", status="committed",
                    idempotency_key=decision_id))
            assert (await probe.read(receipt))[0] == {}
            async with sessions.begin() as session:
                observation = await session.get(LoopObservation, observation_id)
                observation.envelope = {"causal_chain": [{"intent_id": "wanted"}]}
            # Merely having a matching draft decision is insufficient.
            assert (await probe.read(receipt))[0] == {}
            async with sessions.begin() as session:
                decision = await session.get(LoopDecision, decision_id)
                decision.authority_committed_at = datetime.now(UTC)
                session.add(LoopAction(action_id=uuid.uuid4().hex, loop_id=seed["loop_id"], decision_id=decision_id,
                    position=0, action_type=action_type, payload={}, status="applied",
                    result={"context_id": seed["context_id"], "context_revision_id": seed["revision_id"]}))
                publication = await session.get(ContextPublicationReceipt, seed["revision_id"])
                await session.delete(publication)
            milestones, _, _ = await probe.read(receipt)
            assert set(milestones) == {"patrol_decision_committed"}
            async with sessions.begin() as session:
                session.add(ContextPublicationReceipt(revision_id=seed["revision_id"], context_id=seed["context_id"]))
            milestones, _, _ = await probe.read(receipt)
            if action_type == "create_lane":
                assert set(milestones) == {"patrol_decision_committed", "derived_context_available"}
                assert milestones["derived_context_available"]["revision_id"] == seed["revision_id"]
            else:
                assert set(milestones) == {"patrol_decision_committed"}

            async def successor(number):
                round_id, observation_id, successor_id = (uuid.uuid4().hex for _ in range(3))
                async with sessions.begin() as session:
                    session.add(LoopRound(round_id=round_id, loop_id=seed["loop_id"], number=number,
                        status="completed", authority_revision=1, goal_revision=1, frontier_hash="f" * 64, workspace_revision=1))
                    await session.flush()
                    session.add(LoopObservation(observation_id=observation_id, loop_id=seed["loop_id"],
                        round_id=round_id, generation=1,
                        envelope={"causal_chain": [], "mission": {"input_sources": {"outcome": [{"intent_id": "wanted"}]}}},
                        envelope_hash="b" * 64))
                    await session.flush()
                    session.add(LoopDecision(decision_id=successor_id, loop_id=seed["loop_id"], round_id=round_id,
                        observation_id=observation_id, authority_committed_at=datetime.now(UTC), holder_id="patrol",
                        intent={}, rationale="test", status="committed", idempotency_key=successor_id))
                return round_id, successor_id

            # D1 accepts, D2 publishes a Context, D3 starts that same revision.
            _, creation_id = await successor(2)
            async with sessions.begin() as session:
                action = await session.scalar(select(LoopAction).where(LoopAction.decision_id == decision_id))
                action.decision_id = creation_id
                creation_action_id = action.action_id
            dispatch_round, dispatch_id = await successor(3)
            run_id, directive_id = uuid.uuid4().hex, uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(LoopDirective(directive_id=directive_id, loop_id=seed["loop_id"], round_id=dispatch_round,
                    decision_id=dispatch_id, action_id=creation_action_id, target_context_id=seed["context_id"],
                    target_context_revision_id=seed["revision_id"], message_id=uuid.uuid4().hex, content="work",
                    content_hash="c" * 64, actor_id="patrol", grant_id="grant", grant_revision=1, goal_revision=1,
                    correlation_id="wanted", idempotency_key=directive_id))
                await session.flush()
                session.add(DesktopRun(run_id=run_id, task_id=seed["context_id"], agent_id="main", kind="main",
                    status="running", loop_id=seed["loop_id"], round_id=dispatch_round, directive_id=directive_id,
                    context_revision_id=seed["revision_id"]))
                await session.flush()
                # Dispatch admission alone does not mean execution has started.
                session.add(RunDispatch(dispatch_id=uuid.uuid4().hex, run_id=run_id, running_at=datetime.now(UTC)))
            milestones, _, _ = await probe.read(receipt)
            assert milestones["patrol_decision_committed"]["decision_id"] == decision_id
            if action_type == "create_lane":
                assert milestones["derived_context_available"]["decision_id"] == creation_id
            assert "first_run_started" not in milestones
            async with sessions.begin() as session:
                for kind, entity in (("context.run.started", "context_run"), ("context.output.available", "loop_activity")):
                    await LoopEventJournal().append(session, seed["loop_id"], CanonicalEventDraft(
                        kind=kind, entity_type=entity, entity_id=run_id, entity_revision=1,
                        payload={"run_id": run_id}, idempotency_key=f"{kind}:{run_id}"))
            milestones, _, _ = await probe.read(receipt)
            assert milestones["first_run_started"]["decision_id"] == dispatch_id
            assert milestones["first_run_started"]["round_id"] == dispatch_round
            if action_type == "create_lane":
                assert milestones["first_effective_output"]["context_decision_id"] == creation_id
            else:
                assert "derived_context_available" not in milestones
                assert "context_decision_id" not in milestones["first_effective_output"]
            async with sessions() as session:
                dispatch_decision = await session.get(LoopDecision, dispatch_id)
                assert milestones["first_effective_output"]["observation_id"] == dispatch_decision.observation_id
        finally:
            await _stop(seed["service"], seed["loop_id"])
            await engine.dispose()
    asyncio.run(run())
