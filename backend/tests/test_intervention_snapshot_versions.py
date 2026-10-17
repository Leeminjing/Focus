"""本文件对外提供用户输入原子快照、多版本和完整因果历史的真实 PostgreSQL 回归。

输入为隔离工作区、实际受理服务及并发领域状态；输出为不变 Oi/hash、唯一未决轮、已授权轮重叠和精确分页断言。
工作流使用生产服务提交三条输入，在中途修改 Fact，再验证历史没有混入后来修订且正文可完整重建。
示例：python -m pytest backend/tests/test_intervention_snapshot_versions.py -q。
"""

import asyncio
from datetime import UTC, datetime
import json
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from backend.tests.test_workspace_patrol import _setup
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.fact_event_materializer import FactEventMaterializer
from backend.app.desktop.agent_loop.fact_identity import FactIdentityResolver
from backend.app.desktop.agent_loop.fact_lifecycle import FactLifecycleRepository, FactObservation
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.agent_loop.fact_verification import FactActor
from backend.app.desktop.agent_loop.journal_models import LoopJournalSequence, LoopProjectorCursor, LoopReplayRetention
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDecision, LoopObservation, LoopRound
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.patrol_inputs import PatrolInputRequest, VisibleContext
from backend.app.desktop.agent_loop.patrol_read_view import PatrolCausalReadRequest
from backend.app.desktop.agent_loop.projection_recovery import ProjectionRecoveryRepository
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.agent_loop.task_progress.contracts import canonical_hash
from backend.app.desktop.agent_loop.task_progress.models import LoopDecisionInputs, LoopProgressWork
from backend.app.desktop.models import DesktopRun

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_distinct_snapshots_preserve_fact_revisions_and_complete_causal_pages(tmp_path):
    async def run():
        engine, sessions, workspace, intake = await _setup(tmp_path)
        try:
            first = await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="原方向"))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, first["loop_id"])
                fact = await FactLifecycleRepository().observe(session, FactObservation(loop_id=loop.loop_id,
                    identity=FactIdentityResolver.resolve(loop.loop_id, "finding", "测试状态", uuid.uuid4().hex),
                    fact_type="finding", source_context_id=loop.initial_context_id, source_run_id=None,
                    correlation_id=None, presentation={"summary": "未验证"}, evidence=(),
                    observer=FactActor(kind="system", actor_id="test"), occurred_at=datetime.now(UTC)))
                fact_id = fact.fact_id
                run_id = uuid.uuid4().hex
                session.add(DesktopRun(run_id=run_id, task_id=loop.initial_context_id, agent_id="lead_agent",
                    kind="main", status="running", loop_id=loop.loop_id, round_id=first["round_id"]))
                delayed = await LoopEventJournal().append(session, loop.loop_id, CanonicalEventDraft(
                    kind="fact.verification.completed", entity_type="fact_verification", entity_id=uuid.uuid4().hex,
                    entity_revision=1, payload={"fact_id": fact_id, "target_state": "verified"}, idempotency_key=uuid.uuid4().hex))
                session.add(LoopProjectorCursor(loop_id=loop.loop_id, projector_name="loop_facts", last_sequence=delayed.sequence - 1))
                retention = await session.get(LoopReplayRetention, loop.loop_id)
                if retention is None:
                    session.add(LoopReplayRetention(loop_id=loop.loop_id, minimum_sequence=2))
                else:
                    retention.minimum_sequence = 2
                failure = await ProjectionRecoveryRepository().record_failure(session, loop_id=loop.loop_id,
                    projector_name="loop_facts", unit_kind="event", unit_id=uuid.uuid4().hex,
                    error=ValueError("quarantined projection"), boundary_sequence=delayed.sequence)
                failure_id = failure.failure_id
                context_id = loop.initial_context_id
            visible_cache = {"loop_id": first["loop_id"], "live_sequence": 0,
                "fact_refs": [{"fact_id": fact_id, "revision": 1}], "run_previews": [{"run_id": run_id,
                    "context_id": context_id, "sequence": 999999, "role": "assistant", "text": "用户实际看到的片段",
                    "truncated": True, "gap": True}]}
            second_request = PatrolInputRequest(submission_id=uuid.uuid4().hex, content="看到结果后修正" * 90,
                visible_context=VisibleContext.model_validate(visible_cache))
            second = await intake.submit(workspace, second_request)
            visible_cache.clear()
            async with sessions.begin() as session:
                await FactEventMaterializer().materialize(session, delayed)
                (await session.get(LoopProjectorCursor, (first["loop_id"], "loop_facts"))).last_sequence = (
                    await session.get(LoopJournalSequence, first["loop_id"])).last_sequence
            third = await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="第三次修正"))
            assert first["round_id"] == second["round_id"] == third["round_id"]
            assert [first["introduced_generation"], second["introduced_generation"], third["introduced_generation"]] == [1, 2, 3]
            assert await intake.submit(workspace, second_request) == second
            async with sessions() as session:
                rows = tuple(await session.scalars(select(LoopObservation).where(LoopObservation.round_id == first["round_id"]).order_by(LoopObservation.generation)))
                assert len(rows) == 3
                old = rows[1].envelope["pre_input_state"]
                assert old["intervention"]["facts"][0]["state"] == "observed"
                assert old["intervention"]["facts"][0]["revision"] == 1
                assert old["intervention"]["projection"]["cursor"] == delayed.sequence - 1
                assert old["intervention"]["projection"]["lag"] > 0
                visible = old["intervention"]["visible_context"]
                assert visible["status"] == "expired" and visible["live_sequence"] == 0
                assert visible["fact_refs"] == [{"fact_id": fact_id, "revision": 1}]
                assert visible["run_previews"][0]["sequence"] > old["intervention"]["server_sequence"]
                assert visible["run_previews"][0]["text"] == "用户实际看到的片段"
                assert visible["run_previews"][0]["truncated"] and visible["run_previews"][0]["gap"]
                latest = LoopObservationEnvelope.model_validate(rows[2].envelope)
                assert latest.pre_input_state["intervention"]["facts"][0]["revision"] == 3
                coverage = latest.pre_input_state["intervention"]["projection"]
                assert coverage["cursor"] >= delayed.sequence
                assert coverage["failures"][0]["failure_id"] == failure_id and coverage["failures"][0]["status"] == "quarantined"
                assert latest.pre_input_state["intervention"]["visible_context"]["status"] == "unknown"
                assert canonical_hash(old) == second["snapshot_hash"]
                assert [item["intent_id"] for item in latest.causal_chain] == [first["intent_id"], second["intent_id"], third["intent_id"]]
                first_view, second_view, third_view = [item["read_view"] for item in latest.causal_chain]
                assert first_view["comparison"] == "baseline_only"
                assert second_view["snapshot_hash"] == second["snapshot_hash"]
                assert third_view["previous"]["intent_id"] == second["intent_id"]
                assert third_view["previous"]["snapshot_hash"] == second["snapshot_hash"]
                assert second_view["visible_context"] == visible
                referenced, = second_view["referenced_facts"]
                assert referenced["fact_id"] == fact_id and referenced["revision"] == 1
                assert referenced["state"] == "observed" and referenced["presentation"] == {"summary": "未验证"}
                assert any(change["path"] == ["intervention", "facts", {"fact_id": fact_id}, "revision"]
                           and change["before"] == 1 and change["after"] == 3 for change in third_view["changes"])
                assert await session.scalar(select(func.count()).select_from(LoopDecisionInputs).where(LoopDecisionInputs.round_id == first["round_id"])) == 3
                assert await session.scalar(select(func.count()).select_from(LoopProgressWork).where(LoopProgressWork.loop_id == first["loop_id"])) == 0
            reader, cursor, content = LoopObservationService(sessions, None), 0, ""
            while cursor is not None:
                page, = await reader.selective_read(latest, (PatrolCausalReadRequest(
                    intent_id=second["intent_id"], snapshot_hash=second["snapshot_hash"], cursor=cursor, max_bytes=256),))
                content += page["content"]
                cursor = page["next_cursor"]
            assert json.loads(content) == old
            with pytest.raises(ValueError):
                await reader.selective_read(latest, (PatrolCausalReadRequest(
                    intent_id=uuid.uuid4().hex, snapshot_hash=second["snapshot_hash"]),))
            with pytest.raises(ValueError):
                await reader.selective_read(latest, (PatrolCausalReadRequest(
                    intent_id=second["intent_id"], snapshot_hash=third["snapshot_hash"]),))
            fourth = await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex,
                content="仍然引用第一版", visible_context=VisibleContext(loop_id=first["loop_id"],
                    fact_refs=({"fact_id": fact_id, "revision": 1},))))
            async with sessions() as session:
                fourth_row = await session.get(LoopObservation, fourth["observation_id"])
                assert fourth_row.envelope["intervention"]["facts"][0]["revision"] == 3
                view = fourth_row.envelope["causal_chain"][-1]["read_view"]
                assert view["referenced_facts"] == second_view["referenced_facts"]
                assert canonical_hash((await session.get(LoopObservation, second["observation_id"])).envelope["pre_input_state"]) == second["snapshot_hash"]
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_committed_unfinished_round_is_fully_visible_and_new_round_merges_inputs(tmp_path):
    async def run():
        engine, sessions, workspace, intake = await _setup(tmp_path)
        try:
            first = await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="第一轮"))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, first["loop_id"])
                round_row = await session.get(LoopRound, first["round_id"])
                decision = LoopDecision(decision_id=uuid.uuid4().hex, loop_id=loop.loop_id,
                    round_id=round_row.round_id, observation_id=first["observation_id"],
                    holder_id=loop.holder_id, intent={}, rationale="测试已授权边界", status="committed",
                    authority_committed_at=datetime.now(UTC), idempotency_key=uuid.uuid4().hex)
                session.add(decision)
                await session.flush()
                round_row.decision_id, round_row.status = decision.decision_id, "waiting_runs"
                session.add_all([DesktopRun(run_id=uuid.uuid4().hex, task_id=loop.initial_context_id,
                    agent_id="lead_agent", kind="main", status="running", loop_id=loop.loop_id,
                    round_id=round_row.round_id, equipment={"access_mode": "read-only", "permissions": ["read"]}) for _ in range(40)])
            second = await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="第二轮介入"))
            third = await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="更新介入"))
            assert first["round_id"] != second["round_id"] == third["round_id"]
            async with sessions() as session:
                observation = await session.get(LoopObservation, second["observation_id"])
                snapshot = observation.envelope["pre_input_state"]["intervention"]
                assert len(snapshot["runs"]) == 40
                assert all(item["access_mode"] == "read-only" for item in snapshot["runs"])
                assert snapshot["rounds"][0]["decision"]["decision_id"] == decision.decision_id
                assert await session.scalar(select(func.count()).select_from(LoopRound).where(LoopRound.loop_id == first["loop_id"])) == 2
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_cross_loop_visible_context_rolls_back_input_and_generation(tmp_path):
    async def run():
        engine, sessions, workspace, intake = await _setup(tmp_path)
        try:
            first = await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="开始"))
            with pytest.raises(HTTPException) as error:
                await intake.submit(workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex,
                    content="无效引用", visible_context=VisibleContext(loop_id=uuid.uuid4().hex, live_sequence=0)))
            assert error.value.status_code == 409
            assert len((await intake.history(workspace))["items"]) == 1
            with pytest.raises(LookupError):
                await LoopObservationService(sessions, None).capture(uuid.uuid4().hex, first["round_id"])
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopObservation).where(LoopObservation.loop_id == first["loop_id"])) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("conflicting_mode", [False, True])
def test_migration_binds_only_existing_authorized_run_without_widening_mode(tmp_path, conflicting_mode):
    async def run():
        import importlib
        import os
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from test_agent_loop_round_liveness import _seed_loop
        from backend.app.desktop.agent_loop.models import LoopAction, LoopDirective

        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            seeded = await _seed_loop(sessions, tmp_path, label="migration", started_at=datetime.now(UTC))
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, seeded["loop_id"])
                loop.equipment = {**loop.equipment, "access_mode": "danger-full-access"}
                decision = LoopDecision(decision_id=uuid.uuid4().hex, loop_id=loop.loop_id,
                    round_id=seeded["round_id"], holder_id=loop.holder_id, intent={}, rationale="legacy authority",
                    status="committed", authority_committed_at=datetime.now(UTC), idempotency_key=uuid.uuid4().hex)
                session.add(decision)
                await session.flush()
                (await session.get(LoopRound, seeded["round_id"])).decision_id = decision.decision_id
                action = LoopAction(action_id=uuid.uuid4().hex, decision_id=decision.decision_id,
                    loop_id=loop.loop_id, position=0, action_type="continue_context", payload={}, status="committed")
                session.add(action)
                await session.flush()
                directive = LoopDirective(directive_id=uuid.uuid4().hex, loop_id=loop.loop_id,
                    round_id=seeded["round_id"], decision_id=decision.decision_id, action_id=action.action_id,
                    target_context_id=seeded["context_id"], target_context_revision_id=seeded["revision_id"],
                    message_id=uuid.uuid4().hex, content="existing instruction", content_hash="a" * 64,
                    actor_id=loop.holder_id, grant_id=seeded["snapshot"]["grant"]["grant_id"],
                    grant_revision=1, goal_revision=1, status="authorized", lifecycle_state="authorized",
                    correlation_id=decision.decision_id, idempotency_key=uuid.uuid4().hex)
                session.add(directive)
                await session.flush()
                run_row = DesktopRun(run_id=uuid.uuid4().hex, task_id=seeded["context_id"], agent_id="lead_agent",
                    kind="main", status="running", loop_id=loop.loop_id, round_id=seeded["round_id"],
                    directive_id=directive.directive_id, equipment={"access_mode": "read-only", "permissions": ["read"], "model_name": "unchanged"})
                session.add(run_row)
                conflicting_run_id = None
                if conflicting_mode:
                    conflicting_run_id = uuid.uuid4().hex
                    session.add(DesktopRun(run_id=conflicting_run_id, task_id=seeded["context_id"], agent_id="lead_agent",
                        kind="main", status="running", loop_id=loop.loop_id, round_id=seeded["round_id"],
                        directive_id=directive.directive_id, equipment={"access_mode": "workspace-write", "permissions": ["read"]}))
                await session.flush()
                run_id, decision_id = run_row.run_id, decision.decision_id
            migration = importlib.import_module("focus.persistence.migrations.versions.a70d1e2f3b4c_round_observation_versions")

            def migrate(connection):
                with Operations.context(MigrationContext.configure(connection)):
                    migration._historical_permissions()
            async with engine.begin() as connection:
                await connection.run_sync(migrate)
            async with sessions() as session:
                run_row = await session.get(DesktopRun, run_id)
                decision = await session.get(LoopDecision, decision_id)
                assert run_row.equipment["access_mode"] == "read-only"
                assert run_row.equipment["permissions"] == ["read"] and run_row.equipment["model_name"] == "unchanged"
                assert run_row.equipment["round_authorization"] == {"loop_id": seeded["loop_id"],
                    "round_id": seeded["round_id"], "decision_id": decision_id,
                    "context_id": seeded["context_id"], "grant_revision": 1, "access_mode": "read-only", "source": "migration"}
                assert decision.intent["context_permissions"] == [{"context_id": seeded["context_id"], "access_mode": "read-only"}]
                if conflicting_run_id:
                    from backend.app.desktop.agent_loop.directive_equipment import round_permission_state

                    conflicting_run = await session.get(DesktopRun, conflicting_run_id)
                    assert conflicting_run.equipment["access_mode"] == "workspace-write"
                    assert conflicting_run.equipment["round_authorization"]["migration_conflict"] == {
                        "reason": "inconsistent_context_access_modes", "selected_mode": "read-only",
                        "observed_modes": ["read-only", "workspace-write"]}
                    assert (await round_permission_state(session, conflicting_run))["valid"] is False
                    assert (await round_permission_state(session, run_row))["valid"] is True
                assert all("round_authorization" not in item.equipment for item in await session.scalars(
                    select(DesktopRun).where(DesktopRun.task_id == seeded["context_id"], DesktopRun.directive_id.is_(None))))
        finally:
            await engine.dispose()
    asyncio.run(run())
