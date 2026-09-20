r"""本文件验证后端 LoopLiveProjection schema、实体 reducers、单边界 snapshot、rebuild 与 lag diagnostics。

输入为 active/idle/paused/completed 状态、重复/陈旧/缺口事件和隔离 PostgreSQL journal；输出为确定性实体状态、
有界 timeline、byte-equivalent rebuild、cursor 与零 lag 断言。具体工作流为先纯归约合同，再追加真实事件并投影。
示例：`pytest backend/tests/test_loop_live_projection.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft, CanonicalEventEnvelope
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.journal_models import LoopProjectorCursor
from backend.app.desktop.agent_loop.live_api import LoopLiveEventFeed
from backend.app.desktop.agent_loop.live_projection_contract import LoopLiveProjection, ProjectedEntity
from backend.app.desktop.agent_loop.live_projection_projector import LoopLiveSnapshotProjector
from backend.app.desktop.agent_loop.live_projection_reducer import LoopLiveProjectionReducer, ProjectionSequenceGap
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.models import DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@pytest.mark.parametrize("status", ["running", "idle", "paused", "completed"])
def test_projection_serializes_loop_lifecycle(status: str) -> None:
    projection = LoopLiveProjection(loop_id="l1", last_sequence=1, loop=ProjectedEntity(entity_id="l1", revision=1, updated_sequence=1, state={"status": status}))
    assert projection.model_dump(mode="json")["loop"]["state"]["status"] == status


def test_reducer_is_deterministic_for_duplicates_stale_revisions_and_gaps() -> None:
    reducer = LoopLiveProjectionReducer(timeline_limit=2)
    state = LoopLiveProjection(loop_id="l1")
    first = _envelope(1, "context.updated", "context", "c1", 2, {"status": "running"})
    stale = _envelope(2, "context.updated", "context", "c1", 1, {"status": "queued"})
    state = reducer.reduce(state, first)
    assert reducer.reduce(state, first) == state
    state = reducer.reduce(state, stale)
    assert state.contexts["c1"].revision == 2
    state = reducer.reduce(state, _envelope(3, "future.widget.changed", "alien", "a1", 1, {"summary": "future"}))
    assert len(state.activity_timeline) == 2
    assert state.unknown_kinds == ("future.widget.changed",)
    with pytest.raises(ProjectionSequenceGap):
        reducer.reduce(LoopLiveProjection(loop_id="l1", last_sequence=1), _envelope(3, "context.updated", "context", "c2", 1, {}))


def test_snapshot_projector_and_rebuild_share_one_committed_boundary(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        loop_id = await _seed_loop(sessions, tmp_path)
        journal = LoopEventJournal()
        drafts = (
            CanonicalEventDraft(kind="loop.lifecycle.changed", entity_type="loop", entity_id=loop_id, entity_revision=1, payload={"status": "running", "health": "observing"}, idempotency_key="loop:1"),
            CanonicalEventDraft(kind="patrol.session.phase_changed", entity_type="patrol_session", entity_id="patrol-1", entity_revision=1, payload={"phase": "observing"}, idempotency_key="patrol:1"),
            CanonicalEventDraft(kind="context.lifecycle.changed", entity_type="context", entity_id="context-1", entity_revision=1, payload={"status": "running"}, idempotency_key="context:1"),
            CanonicalEventDraft(kind="context.run.started", entity_type="run", entity_id="run-1", entity_revision=1, payload={"status": "running"}, idempotency_key="run:1"),
            CanonicalEventDraft(kind="context.tool.completed", entity_type="tool", entity_id="tool-1", entity_revision=1, payload={"status": "success"}, idempotency_key="tool:1"),
            CanonicalEventDraft(kind="context.workspace.changed", entity_type="workspace_change", entity_id="workspace-1", entity_revision=1, payload={"status": "changed"}, idempotency_key="workspace:1"),
            CanonicalEventDraft(kind="curator.assignment.proposed", entity_type="curator", entity_id="curator-1", entity_revision=1, payload={"status": "proposed"}, idempotency_key="curator:1"),
            CanonicalEventDraft(kind="directive.authorized", entity_type="directive", entity_id="directive-1", entity_revision=1, payload={"status": "authorized"}, idempotency_key="directive:1"),
            CanonicalEventDraft(kind="fact.upserted", entity_type="fact", entity_id="fact-1", entity_revision=1, payload={"status": "observed"}, idempotency_key="fact:1"),
            CanonicalEventDraft(kind="fact.upserted", entity_type="fact", entity_id="fact-1", entity_revision=2, payload={"status": "verified"}, idempotency_key="fact:2"),
            CanonicalEventDraft(kind="portfolio.published", entity_type="portfolio", entity_id="portfolio-1", entity_revision=1, payload={"status": "published"}, idempotency_key="portfolio:1"),
        )
        try:
            async with sessions.begin() as session:
                for draft in drafts:
                    await journal.append(session, loop_id, draft)
            batch = await LoopLiveEventFeed(sessions).read_batch(loop_id, 0)
            assert [event["sequence"] for event in batch["events"]] == list(range(1, len(drafts) + 1))
            assert {event["kind"] for event in batch["events"]} >= {"patrol.session.phase_changed", "curator.assignment.proposed", "directive.authorized", "context.run.started", "context.tool.completed", "context.workspace.changed", "fact.upserted", "portfolio.published"}
            projector = LoopLiveSnapshotProjector(page_size=3)
            async with sessions.begin() as session:
                snapshot = await projector.project(session, loop_id)
            assert snapshot.last_sequence == len(drafts)
            assert snapshot.diagnostics.lag == 0
            assert snapshot.facts["fact-1"].state["status"] == "verified"
            assert snapshot.contexts["context-1"].state["status"] == "running"
            async with sessions.begin() as session:
                rebuilt = await projector.rebuild(session, loop_id)
                cursor = await session.get(LoopProjectorCursor, (loop_id, "live_snapshot"))
            assert snapshot.model_dump(mode="json", exclude={"diagnostics"}) == rebuilt.model_dump(mode="json", exclude={"diagnostics"})
            assert cursor.last_sequence == len(drafts)
        finally:
            await engine.dispose()

    asyncio.run(run())


def _envelope(sequence: int, kind: str, entity_type: str, entity_id: str, revision: int, payload: dict) -> CanonicalEventEnvelope:
    return CanonicalEventEnvelope(event_id=f"event-{sequence}", loop_id="l1", sequence=sequence, schema_version=1, kind=kind, entity_type=entity_type, entity_id=entity_id, entity_revision=revision, payload=payload, visibility={}, idempotency_key=f"key-{sequence}", occurred_at=datetime(2026, 1, 1, tzinfo=UTC))


async def _seed_loop(sessions, tmp_path: Path) -> str:
    loop_id = uuid.uuid4().hex
    workspace_id = uuid.uuid4().hex
    context_id = uuid.uuid4().hex
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="projection"))
        await session.flush()
        session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{loop_id[:8]}", title="projection"))
        await session.flush()
        session.add(AgentLoop(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, holder_id="patrol", status="running", health="observing"))
    return loop_id
