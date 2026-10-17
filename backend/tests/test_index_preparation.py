"""局部预热、前台优先调度、授权和阶段进度的离线回归。"""

import asyncio

import pytest
from sqlalchemy import func, select

from backend.app.desktop.agent_loop.context_expansion.index_model_budget import IndexBudgetExceeded
from backend.app.desktop.agent_loop.context_expansion.index_prewarm import LocalIndexPrewarmer
from backend.app.desktop.agent_loop.context_expansion.index_progress import IndexProgressReporter
from backend.app.desktop.agent_loop.context_expansion.index_scheduling import IndexModelSlots
from backend.app.desktop.agent_loop.context_expansion.models import LoopSegmentProjectionRecord, LoopSemanticIndexArtifact
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.live_projection_contract import LoopLiveProjection
from backend.app.desktop.agent_loop.live_projection_reducer import LoopLiveProjectionReducer
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopContextMembership, LoopDelegationGrant
from backend.tests.incremental_index_support import ModelProbe, messages, observation, revision
from backend.tests.test_incremental_revision_index import exercise

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_foreground_overtakes_background_waiters_and_cancel_releases_priority():
    async def run():
        slots = IndexModelSlots(1)
        order = []

        async def request(name, background=False):
            async with slots.slot(background=background):
                order.append(name)

        async with slots.slot(background=True):
            background = asyncio.create_task(request("background", True))
            foreground = asyncio.create_task(request("foreground"))
            cancelled = asyncio.create_task(request("cancelled"))
            await asyncio.sleep(0)
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(background, foreground), 2)
        assert order == ["foreground", "background"]
        assert slots._busy == slots._background == slots._foreground_waiters == 0

    asyncio.run(run())


def test_one_background_call_leaves_capacity_for_foreground():
    async def run():
        slots = IndexModelSlots(4)
        second_entered, foreground_entered = asyncio.Event(), asyncio.Event()

        async def background():
            async with slots.slot(background=True):
                second_entered.set()

        async def foreground():
            async with slots.slot():
                foreground_entered.set()

        async with slots.slot(background=True):
            second = asyncio.create_task(background())
            first = asyncio.create_task(foreground())
            await asyncio.wait_for(foreground_entered.wait(), 1)
            assert not second_entered.is_set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert second_entered.is_set()

    asyncio.run(run())


def test_prewarm_only_saves_local_records_then_current_revision_is_interpreted(tmp_path):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions)
        first = await revision(sessions, seed["context_id"], messages(24))
        prewarmer = LocalIndexPrewarmer(sessions, service)
        assert await prewarmer.drain(seed["loop_id"]) == 1
        assert len(probe.local_calls) == 4 and not probe.interpretation_calls
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(LoopSegmentProjectionRecord).where(
                LoopSegmentProjectionRecord.context_id == seed["context_id"])) == 2
            assert not await session.scalar(select(LoopSemanticIndexArtifact.index_id).where(
                LoopSemanticIndexArtifact.revision_id == first.ref.revision_id))
            assert (await session.get(LoopBudgetUsage, seed["loop_id"])).model_calls == 4
        probe.calls.clear()
        assert await prewarmer.drain(seed["loop_id"]) == 0
        assert probe.calls == []
        # A new service proves this is durable reuse, not an in-memory result.
        built = await probe.service(sessions).build(observation(seed, first))
        assert built.blocker_code is None, built.blocker_summary
        assert len(probe.calls) == 1 and len(probe.interpretation_calls) == 1
        second = await revision(sessions, seed["context_id"], messages(25), parent=first)
        probe.calls.clear()
        built = await service.build(observation(seed, second))
        assert built.blocker_code is None, built.blocker_summary
        assert len(probe.local_calls) == 2 and len(probe.interpretation_calls) == 1
        assert built.indexes[0].source == second.ref
        assert all(r.source == second.ref for u in built.indexes[0].semantic_units for r in u.evidence_refs)
        probe.calls.clear()
        await service.build(observation(seed, second))
        assert not probe.calls

    exercise(tmp_path, run)


@pytest.mark.parametrize("failure", ["unpublished", "scope", "budget", "membership", "paused", "authority"])
def test_prewarm_denies_before_model_call(tmp_path, failure):
    async def run(sessions, seed):
        probe = ModelProbe()
        service = probe.service(sessions)
        target = await revision(sessions, seed["context_id"], messages(2), published=failure != "unpublished")
        async with sessions.begin() as session:
            loop = await session.get(AgentLoop, seed["loop_id"])
            grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id,
                                                                         LoopDelegationGrant.status == "active"))
            authority = loop.authority_revision
            if failure == "scope":
                grant.context_scope = ["different-context"]
            elif failure == "budget":
                grant.budgets = {**grant.budgets, "max_model_calls": 0}
            elif failure == "membership":
                member = await session.scalar(select(LoopContextMembership).where(LoopContextMembership.loop_id == loop.loop_id))
                member.status = "retired"
            elif failure == "paused":
                loop.status = "paused"
            elif failure == "authority":
                authority += 1
        with pytest.raises(IndexBudgetExceeded):
            await service.prewarm_revision(seed["loop_id"], authority, target)
        assert probe.calls == []

    exercise(tmp_path, run)


def test_prewarm_rechecks_scope_before_verifier_and_bills_sent_projection(tmp_path):
    async def run(sessions, seed):
        async def gate(role, payload):
            if role == "projector":
                async with sessions.begin() as session:
                    grant = await session.scalar(select(LoopDelegationGrant).where(
                        LoopDelegationGrant.loop_id == seed["loop_id"], LoopDelegationGrant.status == "active"))
                    grant.context_scope = ["different-context"]

        probe = ModelProbe(gate=gate)
        service = probe.service(sessions)
        target = await revision(sessions, seed["context_id"], messages(2))
        with pytest.raises(IndexBudgetExceeded, match="scope"):
            await service.prewarm_revision(seed["loop_id"], 1, target)
        assert [r for r, _ in probe.calls] == ["projector"]
        async with sessions() as session:
            assert (await session.get(LoopBudgetUsage, seed["loop_id"])).model_calls == 1
            assert not await session.scalar(select(LoopSegmentProjectionRecord.record_id).where(
                LoopSegmentProjectionRecord.context_id == seed["context_id"]))

    exercise(tmp_path, run)


def test_foreground_reuses_inflight_prewarm_segment_without_duplicate_sampling(tmp_path):
    async def run(sessions, seed):
        started, release = asyncio.Event(), asyncio.Event()

        async def gate(role, payload):
            if role == "projector":
                started.set()
                await release.wait()

        probe = ModelProbe(gate=gate)
        service = probe.service(sessions)
        target = await revision(sessions, seed["context_id"], messages(2))
        warming = asyncio.create_task(service.prewarm_revision(seed["loop_id"], 1, target))
        await asyncio.wait_for(started.wait(), 5)
        foreground = asyncio.create_task(service.build(observation(seed, target)))
        await asyncio.sleep(0)
        release.set()
        _, built = await asyncio.wait_for(asyncio.gather(warming, foreground), 10)
        assert built.blocker_code is None, built.blocker_summary
        assert len(probe.local_calls) == 2 and len(probe.interpretation_calls) == 1
        assert not service._segment_locks._entries

    exercise(tmp_path, run)


def test_progress_is_replayable_and_old_observation_is_ignored(tmp_path):
    async def run(sessions, seed):
        target = await revision(sessions, seed["context_id"], messages(2))
        obs = observation(seed, target)
        reporter = IndexProgressReporter(sessions, obs)
        await reporter.report(target.ref.revision_id, "local", completed_segments=1, total_segments=2)
        await reporter.report(target.ref.revision_id, "interpretation", status="queued")
        async with sessions() as session:
            rows = tuple(await session.scalars(select(LoopJournalEvent).where(
                LoopJournalEvent.loop_id == seed["loop_id"], LoopJournalEvent.kind == "semantic_index.progress"
            ).order_by(LoopJournalEvent.sequence)))
        assert len(rows) == 2
        # Use the public journal envelope and reducer, as reconnect snapshots do.
        from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
        async with sessions() as session:
            events = await LoopEventJournal().read(session, seed["loop_id"], after_sequence=0, limit=1000)
        projection = LoopLiveProjection(loop_id=seed["loop_id"])
        for event in events:
            projection = LoopLiveProjectionReducer().reduce(projection, event)
        last = next(a for a in reversed(projection.activity_timeline) if a.kind == "semantic_index.progress")
        assert last.detail["observation_id"] == seed["observation_id"]
        assert last.detail["index_phase"] == "interpretation"
        obs.decision_inputs_ref = "old-observation"
        await reporter.report(index_phase="complete", status="blocked")
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(LoopJournalEvent).where(
                LoopJournalEvent.loop_id == seed["loop_id"], LoopJournalEvent.kind == "semantic_index.progress")) == 2

    exercise(tmp_path, run)
