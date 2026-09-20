r"""本文件验证 Canonical Loop Event Journal 的迁移、合同、原子并发、幂等、replay、retention 与安全投递。

输入为隔离 PostgreSQL、并发 producer、未来事件 kind、过期 cursor 和含秘密字段的 payload；输出为连续 sequence、
严格分页、snapshot-required、去重及授权脱敏断言。具体工作流为建立最小 Loop，使用独立事务并发 append，
再按 cursor 读取与清理。示例：`pytest backend/tests/test_loop_event_journal.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from pydantic import ValidationError
import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft, CanonicalEventEnvelope, EventNotAuthorized, EventVisibility, LiveEventAuthorizationPolicy
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal, ReplayUnavailable, StaleEntityRevision
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.models import DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_event_journal_migration_round_trips_with_existing_loop(tmp_path: Path) -> None:
    migrations = Path(__file__).parents[1] / "packages" / "harness" / "focus" / "persistence" / "migrations" / "alembic.ini"
    config = Config(str(migrations))
    database_url = make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg")
    engine = create_engine(database_url)
    loop_id = uuid.uuid4().hex
    try:
        command.downgrade(config, "a4b5c6d7e8f9")
        with Session(engine) as session, session.begin():
            workspace = DesktopWorkspace(workspace_id=uuid.uuid4().hex, path=str(tmp_path), display_name="journal")
            session.add(workspace)
            session.flush()
            context = DesktopThread(task_id=uuid.uuid4().hex, workspace_id=workspace.workspace_id, thread_id=f"thread-{loop_id[:8]}", title="journal")
            session.add(context)
            session.flush()
            session.add(AgentLoop(loop_id=loop_id, workspace_id=workspace.workspace_id, initial_context_id=context.task_id, holder_id="patrol", status="running", health="observing"))
        command.upgrade(config, "head")
        tables = set(inspect(engine).get_table_names())
        assert {"loop_journal_sequences", "loop_journal_events", "loop_replay_retention", "loop_projector_cursors"} <= tables
        with Session(engine) as session:
            assert session.get(AgentLoop, loop_id) is not None
        command.downgrade(config, "a4b5c6d7e8f9")
        tables = set(inspect(engine).get_table_names())
        assert "loop_journal_events" not in tables
        assert "agent_loops" in tables
    finally:
        command.upgrade(config, "head")
        engine.dispose()


def test_event_contract_accepts_future_kinds_and_rejects_unsafe_payloads() -> None:
    future = CanonicalEventDraft(kind="future.widget.changed", entity_type="widget", entity_id="w1", entity_revision=1, idempotency_key="future:w1:1", payload={"safe": [1, True, None]})
    assert future.schema_version == 1
    with pytest.raises(ValidationError, match="JSON"):
        CanonicalEventDraft(kind="future.widget.changed", entity_type="widget", entity_id="w1", entity_revision=1, idempotency_key="unsafe", payload={"raw": b"secret"})


def test_event_authorization_redacts_secrets_and_evidence() -> None:
    envelope = CanonicalEventEnvelope(event_id="e1", loop_id="l1", sequence=1, schema_version=2, kind="context.tool.completed", entity_type="tool", entity_id="t1", entity_revision=1, visibility=EventVisibility(required_permissions=("read",), evidence_fields=("evidence",)), payload={"status": "success", "token": "secret", "nested": {"chain_of_thought": "hidden", "safe": "visible"}, "evidence": "private output"}, idempotency_key="e1", occurred_at=datetime.now(UTC))
    policy = LiveEventAuthorizationPolicy()
    redacted = policy.redact(envelope, {"read"})
    assert redacted.payload == {"status": "success", "nested": {"safe": "visible"}}
    assert policy.redact(envelope, {"read", "view_evidence"}).payload["evidence"] == "private output"
    with pytest.raises(EventNotAuthorized):
        policy.redact(envelope, set())


def test_concurrent_append_replay_idempotency_and_retention(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        loop_id = await _seed_loop(sessions, tmp_path)
        other_loop_id = await _seed_loop(sessions, tmp_path)
        journal = LoopEventJournal()

        async def append(index: int):
            async with sessions.begin() as session:
                return await journal.append(session, loop_id, _draft(index))

        try:
            events = await asyncio.gather(*(append(index) for index in range(12)))
            assert sorted(event.sequence for event in events) == list(range(1, 13))
            async with sessions.begin() as session:
                isolated = await journal.append(session, other_loop_id, _draft(99))
                assert isolated.sequence == 1
            async with sessions.begin() as session:
                retry = await journal.append(session, loop_id, _draft(3))
            assert retry.event_id == next(event.event_id for event in events if event.entity_id == "run-3")
            async with sessions() as session:
                first = await journal.read(session, loop_id, after_sequence=0, limit=5)
                second = await journal.read(session, loop_id, after_sequence=first[-1].sequence, limit=20)
                other = await journal.read(session, other_loop_id, after_sequence=0)
            assert [item.sequence for item in (*first, *second)] == list(range(1, 13))
            assert [item.entity_id for item in other] == ["run-99"]
            async with sessions.begin() as session:
                settled = await journal.append(session, loop_id, CanonicalEventDraft(kind="context.run.settled", entity_type="run", entity_id="run-3", entity_revision=2, idempotency_key="run-3:settled"))
                assert settled.sequence == 13
            async with sessions.begin() as session:
                with pytest.raises(StaleEntityRevision):
                    await journal.append(session, loop_id, CanonicalEventDraft(kind="context.run.failed", entity_type="run", entity_id="run-3", entity_revision=1, idempotency_key="stale"))
            async with sessions.begin() as session:
                await journal.configure_retention(session, loop_id, 0)
                removed = await journal.prune(session, loop_id, now=datetime.now(UTC) + timedelta(seconds=1))
                assert removed == 13
            async with sessions() as session:
                with pytest.raises(ReplayUnavailable) as error:
                    await journal.read(session, loop_id, after_sequence=0)
                assert error.value.minimum_sequence == 14
                assert await journal.read(session, loop_id, after_sequence=13) == ()
                assert list((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop_id))).all()) == []
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _seed_loop(sessions, tmp_path: Path) -> str:
    loop_id = uuid.uuid4().hex
    workspace_id = uuid.uuid4().hex
    context_id = uuid.uuid4().hex
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path / workspace_id), display_name="journal"))
        await session.flush()
        session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{loop_id[:8]}", title="journal"))
        await session.flush()
        session.add(AgentLoop(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, holder_id="patrol", status="running", health="observing"))
    return loop_id


def _draft(index: int) -> CanonicalEventDraft:
    return CanonicalEventDraft(kind="context.run.started", entity_type="run", entity_id=f"run-{index}", entity_revision=1, correlation_id="round-1", payload={"status": "running", "index": index}, idempotency_key=f"run-{index}:started")
