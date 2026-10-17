"""本文件对外提供 Live 规模优化的 PostgreSQL 回归测试。

输入为多页 journal、损坏检查点、裁剪历史与长期领域实体；输出为增量补放、单调缓存、权限隔离和有界读取断言。
具体工作流为在隔离数据库模拟重开、并发顺序与保留边界，验证未完成工作仍可见。
示例：`python -m pytest backend/tests/test_loop_live_scaling.py -q`。"""

import asyncio
from datetime import UTC, datetime, timedelta
import os
import uuid

import pytest
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test_loop_live_projection import _seed_loop
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft, EventVisibility
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.agent_loop.context_expansion.models import LoopContextExpansion
from backend.app.desktop.agent_loop.journal_models import LoopJournalSequence, LoopLiveCheckpoint
from backend.app.desktop.agent_loop.live_api import LoopLiveSnapshotService
from backend.app.desktop.agent_loop.live_checkpoint import LoopLiveCheckpointStore
from backend.app.desktop.agent_loop.live_history import LoopLiveHistoryService
from backend.app.desktop.agent_loop.live_projection_projector import LoopLiveSnapshotProjector
from backend.app.desktop.agent_loop.models import LoopDelegationGrant, LoopRound

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _append(sessions, loop_id, number):
    async with sessions.begin() as session:
        await LoopEventJournal(enabled=True).append(session, loop_id, CanonicalEventDraft(
            kind="loop.lifecycle.changed", entity_type="loop", entity_id=loop_id,
            entity_revision=number, payload={"status": "running", "summary": f"event {number}"},
            idempotency_key=f"event:{number}"))


def test_checkpoint_reopens_from_sequence_and_cannot_regress(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            loop_id = await _seed_loop(sessions, tmp_path)
            for number in range(1, 8):
                await _append(sessions, loop_id, number)
            first = LoopLiveSnapshotService()
            async with sessions() as session:
                assert (await first.read(session, loop_id))["activity_timeline"] == []
            await first.save_checkpoint(sessions)
            async with sessions() as session:
                checkpoint = await LoopLiveCheckpointStore().load(session, loop_id, 7)
                assert checkpoint.last_sequence == 7
                assert await LoopLiveCheckpointStore().load(session, loop_id, 6) is None
            await _append(sessions, loop_id, 8)
            reads = []

            class Journal(LoopEventJournal):
                async def read(self, session, target, after_sequence, limit):
                    reads.append(after_sequence)
                    return await super().read(session, target, after_sequence, limit)

            service = LoopLiveSnapshotService(LoopLiveSnapshotProjector(journal=Journal(), page_size=2))
            async with sessions() as session:
                snapshot = await service.read(session, loop_id)
            assert reads == [7]
            assert snapshot["last_sequence"] == 8
            assert not snapshot["diagnostics"]["rebuilt"]
            await service.save_checkpoint(sessions)
            await first.save_checkpoint(sessions)
            async with sessions() as session:
                stored = await session.get(LoopLiveCheckpoint, loop_id)
                assert stored.last_sequence == 8
                assert stored.projection["activity_timeline"] == []
                assert (await session.get(LoopJournalSequence, loop_id)).last_sequence == 8
            reads.clear()
            async with sessions() as session:
                assert (await service.read(session, loop_id))["last_sequence"] == 8
            assert reads == []
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("damage", ["schema", "payload", "identity", "sequence"])
def test_invalid_checkpoint_rebuilds_and_is_repaired(tmp_path, damage):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            loop_id = await _seed_loop(sessions, tmp_path)
            await _append(sessions, loop_id, 1)
            service = LoopLiveSnapshotService()
            async with sessions() as session:
                await service.read(session, loop_id)
            await service.save_checkpoint(sessions)
            async with sessions.begin() as session:
                row = await session.get(LoopLiveCheckpoint, loop_id)
                if damage == "schema":
                    row.schema_version = 999
                elif damage == "payload":
                    row.projection = {"invalid": True}
                elif damage == "identity":
                    row.projection = {**row.projection, "loop_id": "other"}
                else:
                    row.projection = {**row.projection, "last_sequence": 0}
            async with sessions() as session:
                snapshot = await service.read(session, loop_id)
            assert snapshot["last_sequence"] == 1
            assert snapshot["diagnostics"]["rebuilt"]
            await service.save_checkpoint(sessions)
            async with sessions() as session:
                assert (await LoopLiveCheckpointStore().load(session, loop_id, 1)).last_sequence == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_history_pages_sequences_and_redacts_with_current_authority(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            loop_id = await _seed_loop(sessions, tmp_path)
            for number in range(1, 6):
                await _append(sessions, loop_id, number)
            async with sessions.begin() as session:
                await LoopEventJournal(enabled=True).append(session, loop_id, CanonicalEventDraft(
                    kind="context.tool.completed", entity_type="tool", entity_id="private-tool",
                    entity_revision=1, visibility=EventVisibility(required_permissions=("view_evidence",)),
                    payload={"summary": "private summary", "run_id": "private-run"}, idempotency_key="private"))
            history = LoopLiveHistoryService()
            async with sessions() as session:
                first = await history.read(session, loop_id, limit=2)
            assert [item["sequence"] for item in first["items"]] == [5, 6]
            assert first["next_before_sequence"] == 5
            assert "private" not in str(first)
            await _append(sessions, loop_id, 7)
            async with sessions() as session:
                second = await history.read(session, loop_id, first["next_before_sequence"], 2)
                third = await history.read(session, loop_id, second["next_before_sequence"], 2)
            assert [item["sequence"] for item in second["items"]] == [3, 4]
            assert [item["sequence"] for item in third["items"]] == [1, 2]
            assert third["next_before_sequence"] is None
            assert third["last_sequence"] == 7
            async with sessions.begin() as session:
                await LoopEventJournal(enabled=True).append(session, loop_id, CanonicalEventDraft(
                    kind="directive.authorized", entity_type="directive", entity_id="old-directive",
                    entity_revision=1, payload={"target_context_id": "original-context"},
                    idempotency_key="old-directive"))
            async with sessions() as session:
                causal_page = await history.read(session, loop_id, limit=1)
            assert causal_page["items"][0]["detail"]["target_context_id"] == "original-context"
            async with sessions.begin() as session:
                session.add(LoopDelegationGrant(grant_id=uuid.uuid4().hex, loop_id=loop_id, revision=1,
                    holder_id="patrol", capabilities=[], context_scope=[], permission_scope=["view_evidence"], budgets={}))
            async with sessions() as session:
                authorized = await history.read(session, loop_id, before_sequence=7, limit=1)
            assert authorized["items"][0]["summary"] == "private summary"
            async with sessions.begin() as session:
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id))
                grant.permission_scope = []
            async with sessions() as session:
                revoked = await history.read(session, loop_id, before_sequence=7, limit=1)
            assert "private" not in str(revoked)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_current_snapshot_bounds_old_entities_and_retains_unfinished_work(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            loop_id = await _seed_loop(sessions, tmp_path)
            await _append(sessions, loop_id, 1)
            old_round = uuid.uuid4().hex
            old_fact = uuid.uuid4().hex
            now = datetime.now(UTC)
            async with sessions.begin() as session:
                for number in range(70):
                    session.add(LoopRound(round_id=old_round if number == 0 else uuid.uuid4().hex,
                        loop_id=loop_id, number=number, status="executing" if number == 0 else "settled",
                        authority_revision=1, goal_revision=1, frontier_hash="frontier", workspace_revision=0))
                await session.flush()
                for number in range(230):
                    stamp = now + timedelta(seconds=number)
                    session.add(LoopFact(fact_id=old_fact if number == 0 else uuid.uuid4().hex, loop_id=loop_id,
                        identity_key=str(number), fact_type="test", normalized_subject=str(number), current_revision=1,
                        state="verifying" if number == 0 else "verified", occurred_at=stamp, updated_at=stamp))
                    session.add(LoopContextExpansion(expansion_id=uuid.uuid4().hex, opportunity_id=str(number),
                        loop_id=loop_id, round_id=old_round,
                        state="signals_collected" if number == 0 else "blocked", revision=1,
                        policy_version="v1", level="context", independence_key=str(number),
                        semantic_fingerprint=str(number), workspace_mode="read_only", opportunity={},
                        safe_summary="expansion", correlation_id=str(number), created_at=stamp, updated_at=stamp))
            async with sessions() as session:
                snapshot = await LoopLiveSnapshotService().read(session, loop_id)
            assert len(snapshot["rounds"]) == 51
            assert old_round in snapshot["rounds"]
            assert len(snapshot["facts"]) == 201
            assert old_fact in snapshot["facts"]
            assert len(snapshot["expansions"]) == 201
            assert snapshot["accounting"]["state"]["accounting"]["legacy_rounds"] == 69
            assert snapshot["accounting"]["state"]["accounting"]["round_history"] == []
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_checkpoint_migration_round_trip():
    from sqlalchemy.engine import make_url

    config = Config(str(Path(__file__).parents[1] / "packages/harness/focus/persistence/migrations/alembic.ini"))
    engine = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg"))
    try:
        assert "loop_live_checkpoints" in inspect(engine).get_table_names()
        command.downgrade(config, "e14b5c6d7e8f")
        assert "loop_live_checkpoints" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        assert "loop_live_checkpoints" in inspect(engine).get_table_names()
    finally:
        command.upgrade(config, "head")
        engine.dispose()


@pytest.mark.parametrize("checkpoint", [False, True])
def test_snapshot_recovers_after_journal_retention(tmp_path, checkpoint):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            loop_id = await _seed_loop(sessions, tmp_path)
            await _append(sessions, loop_id, 1)
            service = LoopLiveSnapshotService()
            if checkpoint:
                async with sessions() as session:
                    await service.read(session, loop_id)
                await service.save_checkpoint(sessions)
            async with sessions.begin() as session:
                journal = LoopEventJournal(enabled=True)
                await journal.configure_retention(session, loop_id, 0)
                assert await journal.prune(session, loop_id, datetime.now(UTC) + timedelta(seconds=1)) == 1
            async with sessions() as session:
                snapshot = await service.read(session, loop_id)
                history = await LoopLiveHistoryService().read(session, loop_id)
            assert snapshot["last_sequence"] == 1
            assert snapshot["loop"]["state"]["status"] == "running"
            assert history["items"] == []
            assert history["minimum_sequence"] == 2
            assert history["truncated"]
        finally:
            await engine.dispose()
    asyncio.run(run())
