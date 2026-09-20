r"""本文件验证版本化 Loop Fact 的确定性 identity、验证权力、增量事件、关系历史与 projector 恢复。

输入为真实 PostgreSQL Loop/Run、测试 ToolMessage、规范 Run-settled 事件及重复投影；输出为 observed→verifying→verified
修订、同 correlation 的 tool/fact 链、显式 contradiction、无重复恢复和 byte-equivalent current projection。
具体工作流为创建最小 Context，启动 Loop，投影一次 Run，重置物化表与 cursor 后从 journal 重建。示例：
`pytest test_loop_materialized_facts.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

from alembic import command
from alembic.config import Config
from langchain_core.messages import HumanMessage, ToolMessage
import pytest
from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop import AgentLoopService, LoopCreateRequest
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.fact_models import LoopFact, LoopFactRelationship, LoopFactRevision
from backend.app.desktop.agent_loop.fact_identity import FactIdentityResolver
from backend.app.desktop.agent_loop.fact_lifecycle import FactLifecycleRepository, FactTransitionRejected
from backend.app.desktop.agent_loop.fact_projector import FactProjector
from backend.app.desktop.agent_loop.fact_verification import FactActor, FactEvidenceReference, FactVerificationDecision, FactVerificationPolicy
from backend.app.desktop.agent_loop.fact_projection import LoopFactProjectionService
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent, LoopProjectorCursor, LoopReplayRetention
from backend.app.desktop.agent_loop.live_api import LoopLiveEventFeed, LoopLiveSnapshotService
from backend.app.desktop.agent_loop.materialized_fact_query import FactParityService, MaterializedFactQueryService
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant
from backend.app.desktop.context_evolution import ContextRevisionContract, ContextRevisionOriginKind, ContextRevisionPayloadMode, ContextRevisionProjectionStatus, ContextRevisionRef, ContextRevisionRepository
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _Checkpointer:
    async def aget_tuple(self, config):
        checkpoint_id = config["configurable"]["checkpoint_id"]
        test_output = "14 passed, 0 failed" if checkpoint_id.endswith("-next") else "12 passed, 2 failed"
        return SimpleNamespace(
            config={"configurable": {"checkpoint_id": checkpoint_id}},
            checkpoint={"channel_values": {"messages": [
                HumanMessage(content="Run tests.", id=f"human-{checkpoint_id}"),
                ToolMessage(content=test_output, tool_call_id=f"call-{checkpoint_id}", name="pytest", id=f"tool-{checkpoint_id}"),
            ]}},
            metadata={},
        )


def test_materialized_fact_migration_round_trips_with_existing_loop(tmp_path: Path) -> None:
    migrations = Path(__file__).parents[1] / "packages" / "harness" / "focus" / "persistence" / "migrations" / "alembic.ini"
    config = Config(str(migrations))
    database_url = make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg")
    engine = create_engine(database_url)
    loop_id = uuid.uuid4().hex
    try:
        command.downgrade(config, "f9a0b1c2d3e4")
        with Session(engine) as session, session.begin():
            workspace = DesktopWorkspace(workspace_id=uuid.uuid4().hex, path=str(tmp_path), display_name="fact-migration")
            session.add(workspace)
            session.flush()
            context = DesktopThread(task_id=uuid.uuid4().hex, workspace_id=workspace.workspace_id, thread_id=f"thread-{loop_id[:8]}", title="fact-migration")
            session.add(context)
            session.flush()
            session.add(AgentLoop(loop_id=loop_id, workspace_id=workspace.workspace_id, initial_context_id=context.task_id, holder_id="patrol", status="running", health="observing"))
        command.upgrade(config, "head")
        assert {"loop_facts", "loop_fact_revisions", "loop_fact_relationships"} <= set(inspect(engine).get_table_names())
        with Session(engine) as session:
            assert session.get(AgentLoop, loop_id) is not None
        command.downgrade(config, "f9a0b1c2d3e4")
        assert "loop_facts" not in set(inspect(engine).get_table_names())
        with Session(engine) as session:
            assert session.get(AgentLoop, loop_id) is not None
    finally:
        command.upgrade(config, "head")
        engine.dispose()


def test_fact_identity_and_verification_policy_are_deterministic() -> None:
    first = FactIdentityResolver.resolve("loop", "test", "  PyTest\nSuite  ", "run-1:tool-1")
    repeated = FactIdentityResolver.resolve("loop", "test", "pytest suite", "run-1:tool-1")
    next_observation = FactIdentityResolver.resolve("loop", "test", "pytest suite", "run-2:tool-2")
    assert first == repeated
    assert first.fact_id != next_observation.fact_id
    model_only = FactVerificationPolicy().evaluate((FactEvidenceReference(kind="model_statement", entity_id="message-1"),), FactActor(kind="model", actor_id="context-1"))
    tool_backed = FactVerificationPolicy().evaluate((FactEvidenceReference(kind="tool", entity_id="tool-1", run_id="run-1"),), FactActor(kind="context", actor_id="context-1"))
    assert model_only.target_state == "observed"
    assert model_only.verifier is None
    assert tool_backed.target_state == "verified"
    assert tool_backed.verifier.kind == "tool"


def test_materialized_fact_projection_rebuilds_without_duplicates(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        workspace_id = f"ws-fact-{suffix}"
        context_id = f"context-fact-{suffix}"
        run_id = f"run-fact-{suffix}"
        loop_id = uuid.uuid4().hex
        revision_id = uuid.uuid4().hex
        correlation_id = f"directive-{suffix}"
        workspace_path = tmp_path / workspace_id
        workspace_path.mkdir()
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name="facts"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{suffix}", title="facts"))
                await session.flush()
                ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=f"thread-{suffix}", checkpoint_ns="", checkpoint_id=f"checkpoint-{suffix}", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
                repository = ContextRevisionRepository()
                await repository.insert(session, ContextRevisionContract(ref=ref, content_hash="a" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.RUN_SETTLED, origin_id=run_id, created_at=datetime.now(UTC)))
                await repository.switch_current(session, ref, None)
                session.add(DesktopRun(run_id=run_id, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{suffix}", context_revision_id=revision_id, final_checkpoint_id=f"checkpoint-{suffix}", user_intent_id=correlation_id, workspace_result={"workspace_status": "settled", "revision": 2, "effect_evidence": [{"kind": "workspace_fingerprint_transition", "changed": True, "changed_files": ["src/service.py"]}], "artifacts": ["reports/pytest.xml"]}, settled_at=datetime.now(UTC)))
            service = AgentLoopService(sessions)
            await service.start(LoopCreateRequest(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, initial_run_id=run_id, holder_id=f"patrol-{suffix}", goal="Verify", task_contract="Use evidence", acceptance_criteria=({"criterion_id": "tests", "text": "tests pass"},), capabilities=("continue_context", "request_completion"), context_scope=(context_id,), permission_scope=("read", "view_evidence")))
            async with sessions.begin() as session:
                event = await LoopEventJournal().append(session, loop_id, CanonicalEventDraft(kind="context.run.settled", entity_type="context_run", entity_id=run_id, entity_revision=2, correlation_id=correlation_id, payload={"run_id": run_id, "status": "success"}, idempotency_key=f"run:{run_id}:settled"))
            projector = FactProjector(sessions, _Checkpointer())
            assert await projector.project_loop(loop_id) == 1
            async with sessions() as session:
                page = await MaterializedFactQueryService().read(session, loop_id, context_id=context_id, kind=None, status=None, before=None, limit=20)
                test_fact = await session.scalar(select(LoopFact).where(LoopFact.loop_id == loop_id, LoopFact.fact_type == "test"))
                revisions = tuple((await session.scalars(select(LoopFactRevision).where(LoopFactRevision.fact_id == test_fact.fact_id).order_by(LoopFactRevision.revision))).all())
                causal = tuple((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.correlation_id == correlation_id).order_by(LoopJournalEvent.sequence))).all())
                baseline = _current_projection(page["facts"])
            assert {item["kind"] for item in page["facts"]} >= {"run", "workspace", "artifact", "test", "tool"}
            assert [item.state for item in revisions] == ["observed", "verifying", "verified"]
            assert test_fact.presentation["outcome_status"] == "failed"
            assert {item.kind for item in causal} >= {"context.run.settled", "context.tool.completed", "fact.upserted"}
            assert all(item.correlation_id == correlation_id for item in causal)
            assert await projector.project_loop(loop_id) == 0
            async with sessions.begin() as session:
                with pytest.raises(FactTransitionRejected):
                    await FactLifecycleRepository().apply_verification(session, test_fact.fact_id, FactVerificationDecision(target_state="verifying", reason="regression"), cause_event_id=None)
            next_run_id = f"run-fact-{suffix}-next"
            next_revision_id = uuid.uuid4().hex
            async with sessions.begin() as session:
                next_ref = ContextRevisionRef(context_id=context_id, revision_id=next_revision_id, generation=2, execution_thread_id=f"thread-{suffix}", checkpoint_ns="", checkpoint_id=f"checkpoint-{suffix}-next", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
                await ContextRevisionRepository().insert(session, ContextRevisionContract(ref=next_ref, content_hash="b" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.RUN_SETTLED, origin_id=next_run_id, created_at=datetime.now(UTC)))
                session.add(DesktopRun(run_id=next_run_id, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="delegated_patrol", execution_thread_id=f"thread-{suffix}", context_revision_id=next_revision_id, final_checkpoint_id=f"checkpoint-{suffix}-next", loop_id=loop_id, settled_at=datetime.now(UTC)))
                await LoopEventJournal().append(session, loop_id, CanonicalEventDraft(kind="context.run.settled", entity_type="context_run", entity_id=next_run_id, entity_revision=2, correlation_id=correlation_id, payload={"run_id": next_run_id, "status": "success"}, idempotency_key=f"run:{next_run_id}:settled"))
            assert await projector.project_loop(loop_id) == 1
            async with sessions() as session:
                all_fact_rows = tuple((await session.scalars(select(LoopFact).where(LoopFact.loop_id == loop_id).order_by(LoopFact.source_run_id, LoopFact.fact_type))).all())
                test_facts = tuple((await session.scalars(select(LoopFact).where(LoopFact.loop_id == loop_id, LoopFact.fact_type == "test").order_by(LoopFact.occurred_at, LoopFact.fact_id))).all())
                relationship = await session.scalar(select(LoopFactRelationship).where(LoopFactRelationship.loop_id == loop_id, LoopFactRelationship.relation == "contradicts"))
                page = await MaterializedFactQueryService().read(session, loop_id, context_id=context_id, kind=None, status=None, before=None, limit=40)
                detail = await MaterializedFactQueryService().detail(session, loop_id, test_facts[0].fact_id)
                legacy = await LoopFactProjectionService(_Checkpointer()).read(session, loop_id, context_id=context_id, kind=None, status=None, before=None, limit=40)
                baseline = _current_projection(page["facts"])
            assert [item.state for item in test_facts] == ["contradicted", "verified"], [(item.fact_type, item.source_run_id, item.normalized_subject, item.presentation) for item in all_fact_rows]
            assert relationship.source_fact_id == test_facts[1].fact_id
            assert relationship.target_fact_id == test_facts[0].fact_id
            assert detail["relationships"][0]["relation"] == "contradicts"
            assert FactParityService.compare(legacy["facts"], page["facts"])["equal"] is True
            full_batch = await LoopLiveEventFeed(sessions).read_batch(loop_id, 0)
            assert full_batch["status"] == "events"
            assert [item["sequence"] for item in full_batch["events"]] == sorted(item["sequence"] for item in full_batch["events"])
            assert any(item["kind"] == "fact.upserted" and "evidence" in item["payload"] for item in full_batch["events"])
            replay_cursor = full_batch["events"][len(full_batch["events"]) // 2]["sequence"]
            replay = await LoopLiveEventFeed(sessions).read_batch(loop_id, replay_cursor)
            assert all(item["sequence"] > replay_cursor for item in replay["events"])
            async with sessions.begin() as session:
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active").with_for_update())
                grant.permission_scope = ["read"]
            narrowed_batch = await LoopLiveEventFeed(sessions).read_batch(loop_id, 0)
            assert all("evidence" not in item["payload"] for item in narrowed_batch["events"] if item["kind"] == "fact.upserted")
            assert (await LoopLiveEventFeed(sessions, page_size=1, max_backlog=1).read_batch(loop_id, 0))["status"] == "resync_required"
            async with sessions.begin() as session:
                retention = await session.get(LoopReplayRetention, loop_id, with_for_update=True)
                retention.minimum_sequence = 2
            assert (await LoopLiveEventFeed(sessions).read_batch(loop_id, 0))["status"] == "snapshot_required"
            async with sessions.begin() as session:
                retention = await session.get(LoopReplayRetention, loop_id, with_for_update=True)
                retention.minimum_sequence = 1
            async with sessions.begin() as session:
                snapshot = await LoopLiveSnapshotService().read(session, loop_id)
            assert snapshot["last_sequence"] == narrowed_batch["last_sequence"]
            assert snapshot["loop"]["state"]["status"] == "running"
            assert set(snapshot["facts"]) == {item["fact_id"] for item in page["facts"]}
            assert all("evidence" not in item["state"] for item in snapshot["facts"].values())
            async with sessions() as session:
                revision_count = len(tuple((await session.scalars(select(LoopFactRevision).where(LoopFactRevision.loop_id == loop_id))).all()))
            assert await projector.reconcile(loop_id) == 2
            async with sessions() as session:
                assert len(tuple((await session.scalars(select(LoopFactRevision).where(LoopFactRevision.loop_id == loop_id))).all())) == revision_count
            async with sessions.begin() as session:
                await session.execute(delete(LoopFactRelationship).where(LoopFactRelationship.loop_id == loop_id))
                await session.execute(delete(LoopFactRevision).where(LoopFactRevision.loop_id == loop_id))
                await session.execute(delete(LoopFact).where(LoopFact.loop_id == loop_id))
                cursor = await session.get(LoopProjectorCursor, (loop_id, FactProjector.PROJECTOR_NAME), with_for_update=True)
                cursor.last_sequence = event.sequence - 1
            assert await projector.project_loop(loop_id) == 2
            async with sessions() as session:
                rebuilt = await MaterializedFactQueryService().read(session, loop_id, context_id=context_id, kind=None, status=None, before=None, limit=40)
                events = tuple((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.kind == "fact.upserted"))).all())
                revisions = tuple((await session.scalars(select(LoopFactRevision).where(LoopFactRevision.loop_id == loop_id))).all())
            assert _current_projection(rebuilt["facts"]) == baseline
            assert len(events) == len(revisions)
            await service.control(loop_id, "pause")
            async with sessions.begin() as session:
                paused = await LoopLiveSnapshotService().read(session, loop_id)
            assert paused["loop"]["state"]["status"] == "paused"
            assert paused["loop"]["state"]["health"] == "idle"
            await service.control(loop_id, "resume")
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id, with_for_update=True)
                loop.status = "completed"
                loop.health = "idle"
                loop.revision += 1
                await LoopEventJournal().append(session, loop_id, CanonicalEventDraft(kind="loop.lifecycle.changed", entity_type="loop", entity_id=loop_id, entity_revision=loop.revision, payload={"status": "completed", "health": "idle"}, idempotency_key=f"loop:{loop_id}:completed"))
            async with sessions.begin() as session:
                completed = await LoopLiveSnapshotService().read(session, loop_id)
            assert completed["loop"]["state"]["status"] == "completed"
        finally:
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
            if loop is not None and loop.status in {"running", "paused", "waiting_user"}:
                await service.control(loop_id, "stop")
            await engine.dispose()

    asyncio.run(run())


def _current_projection(facts: list[dict]) -> tuple[tuple, ...]:
    return tuple(sorted((item["fact_id"], item["revision"], item["kind"], item["status"], item["summary"], item["outcome_status"]) for item in facts))
