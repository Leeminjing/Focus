"""Real PostgreSQL coverage for durable, ordered cross-Round work history."""
import asyncio
from datetime import UTC, datetime, timedelta
import os
from types import SimpleNamespace
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.patrol.agent_loop.journal.event_contract import CanonicalEventDraft, EventVisibility
from backend.app.desktop.patrol.agent_loop.journal.event_journal import LoopEventJournal
from backend.app.desktop.patrol.agent_loop.journal.journal_models import LoopJournalEvent, LoopProjectorCursor, LoopReplayRetention
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopDecision, LoopRound, LoopUserIntent
from backend.app.desktop.patrol.agent_loop.governance.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.patrol.agent_loop.journal.timeline_events import record_work_decision
from backend.app.desktop.patrol.agent_loop.queries.query_routes import loop_query_router
from backend.app.desktop.patrol.agent_loop.journal.timeline_models import WorkTimelineEntry
from backend.app.desktop.patrol.agent_loop.journal.timeline_projector import WorkTimelineProjector
from backend.app.desktop.patrol.agent_loop.journal.timeline_query import WorkTimelineQuery
from backend.app.desktop.persistence.models import DesktopThread, DesktopWorkspace

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def seed(sessions, tmp_path):
    async with sessions.begin() as s:
        workspace = DesktopWorkspace(workspace_id=uuid.uuid4().hex, path=str(tmp_path), display_name="Timeline")
        s.add(workspace)
        await s.flush()
        context = DesktopThread(task_id=uuid.uuid4().hex, workspace_id=workspace.workspace_id,
                                thread_id=uuid.uuid4().hex, title="Timeline")
        s.add(context)
        await s.flush()
        loop = AgentLoop(loop_id=uuid.uuid4().hex, workspace_id=workspace.workspace_id,
                         initial_context_id=context.task_id, holder_id="patrol", status="running", health="observing")
        s.add(loop)
        await s.flush()
        rounds = [LoopRound(round_id=uuid.uuid4().hex, loop_id=loop.loop_id, number=n, status="settled",
                            authority_revision=1, goal_revision=1, frontier_hash="h", workspace_revision=1) for n in (1, 2)]
        s.add_all(rounds)
    return loop.loop_id, context.task_id, [r.round_id for r in rounds]


async def emit(sessions, loop_id, kind, entity, payload, revision=1, visibility=None):
    async with sessions.begin() as s:
        return await LoopEventJournal(enabled=True).append(s, loop_id, CanonicalEventDraft(
            kind=kind, entity_type="timeline_test", entity_id=entity, entity_revision=revision,
            idempotency_key=f"{entity}:{revision}", payload=payload,
            visibility=visibility or EventVisibility(),
        ))


def test_cross_round_order_paging_filters_revisions_and_idempotency(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            loop, context, rounds = await seed(sessions, tmp_path)
            old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
            events = [
                await emit(sessions, loop, "intervention.submitted", "idea", {"content": "新的想法"}),
                await emit(sessions, loop, "patrol.decision.committed", "decision", {"round_id": rounds[0], "intent_ids": ["idea"], "context_ids": [context], "summary": "继续现有任务"}),
                await emit(sessions, loop, "fact.upserted", "test", {"fact_type": "test", "fact_id": "test", "revision": 1, "state": "observed", "occurred_at": old, "source_context_id": context, "presentation": {"title": "8 passed"}}),
                await emit(sessions, loop, "patrol.decision.committed", "decision2", {"round_id": rounds[1], "context_ids": [context], "summary": "增加文本引用"}),
                await emit(sessions, loop, "fact.upserted", "test", {"fact_type": "test", "fact_id": "test", "revision": 2, "state": "verified", "occurred_at": old, "source_context_id": context, "presentation": {"title": "8 passed"}}, revision=2),
                await emit(sessions, loop, "context.tool.completed", "tool", {"secret": "hidden"}),
            ]
            projector = WorkTimelineProjector(sessions, page_size=2)
            await asyncio.gather(projector.project_loop(loop), projector.project_loop(loop))
            await projector.project_loop(loop)
            query = WorkTimelineQuery()
            async with sessions() as s:
                latest = await query.read(s, loop, limit=2)
                assert [e["sequence"] for e in latest["entries"]] == [4, 5]
                first = await query.read(s, loop, before=latest["next_before"], limit=2)
                assert [e["sequence"] for e in first["entries"]] == [2, 3]
                increment = await query.read(s, loop, after=1, limit=2)
                assert [e["sequence"] for e in increment["entries"]] == [2, 3]
                assert increment["next_after"] == 3
                tail = await query.read(s, loop, after=increment["next_after"])
                assert [e["sequence"] for e in tail["entries"]] == [4, 5]
                assert tail["next_after"] == 6, "non-display events must not trap the live cursor"
                all_entries = (await query.read(s, loop))["entries"]
                assert [e["source_event_id"] for e in all_entries] == [e.event_id for e in events[:5]]
                assert all_entries[0]["round_id"] is None, "a submitted idea is not assigned to the current Round"
                assert [e["round_number"] for e in all_entries if e["decision_id"]] == [1, 2]
                facts = (await query.read(s, loop, category="fact"))["entries"]
                assert [e["fact_revision"] for e in facts] == [1, 2]
                assert facts[0]["fact_id"] == facts[1]["fact_id"]
                assert facts[0]["occurred_at"] == facts[1]["occurred_at"]
                assert facts[0]["recorded_at"] != facts[0]["occurred_at"]
                assert "验证通过" in facts[1]["title"]
                assert len((await query.read(s, loop, round_id=rounds[1]))["entries"]) == 1
                assert len((await query.read(s, loop, intent_id="idea"))["entries"]) == 2
            # Crash/replay after losing the consumer cursor keeps durable entries identical.
            async with sessions.begin() as s:
                await s.execute(delete(LoopProjectorCursor).where(LoopProjectorCursor.loop_id == loop))
            await projector.project_loop(loop)
            async with sessions() as s:
                assert (await query.read(s, loop))["entries"] == all_entries
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_retention_waits_for_timeline_then_history_survives_source_deletion(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            loop, _, _ = await seed(sessions, tmp_path)
            await emit(sessions, loop, "intervention.submitted", "idea", {"content": "保留历史"})
            journal = LoopEventJournal(enabled=True)
            async with sessions.begin() as s:
                s.add(LoopProjectorCursor(loop_id=loop, projector_name="loop_facts", last_sequence=1))
                await journal.configure_retention(s, loop, 0)
                assert await journal.prune(s, loop, datetime.now(UTC) + timedelta(days=1)) == 0
            await WorkTimelineProjector(sessions).project_loop(loop)
            async with sessions() as s:
                before = await WorkTimelineQuery().read(s, loop)
            async with sessions.begin() as s:
                assert await journal.prune(s, loop, datetime.now(UTC) + timedelta(days=1)) == 1
            async with sessions() as s:
                assert await s.scalar(select(LoopJournalEvent.event_id).where(LoopJournalEvent.loop_id == loop)) is None
                after = await WorkTimelineQuery().read(s, loop)
                assert before["entries"] == after["entries"]
            # Simulate rollout onto an installation whose earlier sources are gone.
            async with sessions.begin() as s:
                await s.execute(delete(WorkTimelineEntry).where(WorkTimelineEntry.loop_id == loop))
                await s.execute(delete(LoopProjectorCursor).where(LoopProjectorCursor.loop_id == loop, LoopProjectorCursor.projector_name == "work_timeline"))
            await WorkTimelineProjector(sessions).project_loop(loop)
            async with sessions() as s:
                gap = (await WorkTimelineQuery().read(s, loop))["entries"]
                assert len(gap) == 1 and gap[0]["kind"] == "timeline.history_gap"
                assert gap[0]["occurred_at"] is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_timeline_http_contract_and_permissions(tmp_path):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"], poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async def setup():
        loop, _, _ = await seed(sessions, tmp_path)
        await emit(sessions, loop, "intervention.submitted", "public", {"content": "public"})
        await emit(sessions, loop, "intervention.submitted", "restricted", {"content": "restricted"}, visibility=EventVisibility(required_permissions=("view_evidence",)))
        await WorkTimelineProjector(sessions).project_loop(loop)
        return loop
    loop = asyncio.run(setup())
    app = FastAPI()
    app.include_router(loop_query_router)
    app.state.desktop_service = SimpleNamespace(session_factory=sessions)
    try:
        with TestClient(app) as client:
            url = f"/desktop/api/agent-loops/{loop}/timeline"
            response = client.get(url)
            assert response.status_code == 200
            assert [e["summary"] for e in response.json()["entries"]] == ["public"]
            assert client.get(url + "?after=0&before=9").status_code == 422
            assert client.get(url + "?context_id=foreign").status_code == 404
            assert client.get(url.replace(loop, "missing")).status_code == 404
            assert client.get(url + "?category=unsupported").status_code == 422
            assert client.get(url + "?limit=201").status_code == 422
    finally:
        asyncio.run(engine.dispose())


def test_projection_page_failure_rolls_back_entries_and_cursor(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            loop, _, _ = await seed(sessions, tmp_path)
            await emit(sessions, loop, "intervention.submitted", "first", {"content": "first"})
            await emit(sessions, loop, "intervention.submitted", "second", {"content": "second"})
            projector = WorkTimelineProjector(sessions)
            original = projector._entry
            async def fail_second(session, event):
                if event.sequence == 2:
                    raise RuntimeError("projection interrupted")
                return await original(session, event)
            projector._entry = fail_second
            with pytest.raises(RuntimeError, match="interrupted"):
                await projector.project_loop(loop)
            async with sessions() as s:
                assert await s.scalar(select(WorkTimelineEntry.entry_id).where(WorkTimelineEntry.loop_id == loop)) is None
                cursor = await s.get(LoopProjectorCursor, (loop, "work_timeline"))
                assert cursor is None or cursor.last_sequence == 0
            projector._entry = original
            await projector.project_loop(loop)
            async with sessions() as s:
                assert len((await WorkTimelineQuery().read(s, loop))["entries"]) == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_real_input_and_decision_sources_preserve_causal_context_links(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            loop, context, rounds = await seed(sessions, tmp_path)
            intent_id, decision_id = uuid.uuid4().hex, uuid.uuid4().hex
            async with sessions.begin() as s:
                intent = LoopUserIntent(intent_id=intent_id, loop_id=loop, scope="portfolio", content="请增加文本引用", correlation_id=intent_id, goal_revision=1, authority_revision=1)
                s.add(intent)
                lifecycle = InterventionLifecycleRepository()
                await lifecycle.register(s, intent)
                await lifecycle.transition(s, intent_id, "accepted")
                intent.observed_round_id = rounds[1]
                await lifecycle.transition(s, intent_id, "observed")
                decision = LoopDecision(decision_id=decision_id, loop_id=loop, round_id=rounds[1], holder_id="patrol",
                                        rationale="派生文本交互分支", intent={"actions": [{"action": "continue_context", "context_id": context}]},
                                        status="committed", idempotency_key=decision_id, authority_committed_at=datetime.now(UTC))
                s.add(decision)
                await s.flush()
                await record_work_decision(s, decision)
                await LoopEventJournal(enabled=True).append(s, loop, CanonicalEventDraft(
                    kind="context.created", entity_type="context", entity_id=context, entity_revision=1,
                    correlation_id=decision_id, payload={"title": "文本交互 Context"}, idempotency_key=f"create:{context}"))
            await WorkTimelineProjector(sessions).project_loop(loop)
            async with sessions() as s:
                entries = (await WorkTimelineQuery().read(s, loop, intent_id=intent_id))["entries"]
                assert [e["category"] for e in entries] == ["user_decision", "user_decision", "user_decision", "context"]
                assert entries[0]["summary"] == "请增加文本引用" and entries[0]["round_id"] is None
                assert entries[-1]["round_id"] == rounds[1]
                assert entries[-1]["context_ids"] == [context]
                assert entries[-2]["references"]["intent_ids"] == [intent_id]
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_timeline_migration_round_trip_preserves_existing_loop(tmp_path):
    config = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    async def setup():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        try:
            return (await seed(async_sessionmaker(engine, expire_on_commit=False), tmp_path))[0]
        finally:
            await engine.dispose()
    loop = asyncio.run(setup())
    engine = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg"))
    try:
        command.downgrade(config, "f57e8f901234")
        assert "work_timeline_entries" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        assert "work_timeline_entries" in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.scalar(select(AgentLoop.loop_id).where(AgentLoop.loop_id == loop)) == loop
        assert not any(fk["referred_table"] == "loop_journal_events" for fk in inspect(engine).get_foreign_keys("work_timeline_entries"))
    finally:
        command.upgrade(config, "head")
        engine.dispose()
