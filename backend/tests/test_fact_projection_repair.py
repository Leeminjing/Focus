"""隔离 PostgreSQL + 生产 HTTP 路由验证事实修复、失败回滚、身份边界和重复请求。

不启动调度器或 Provider；读取真实 Run/Fact/cursor/outcome，区分事实重建与业务重跑。
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import os
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.fact_models import LoopFact, LoopFactRelationship, LoopFactRevision
from backend.app.desktop.agent_loop.fact_projector import FactProjector
from backend.app.desktop.agent_loop.journal_models import LoopProjectorCursor
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure, LoopProjectionUnitOutcome
from backend.app.desktop.agent_loop.projection_recovery import ProjectionRecoveryRepository
from backend.app.desktop.agent_loop.projection_routes import projection_router
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@asynccontextmanager
async def environment(tmp_path):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id, context_id, loop_id, other_loop, run_id = [uuid4().hex for _ in range(5)]
    try:
        async with sessions.begin() as session:
            session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="repair"))
            await session.flush()
            session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=context_id, title="repair"))
            await session.flush()
            session.add_all([AgentLoop(loop_id=identity, workspace_id=workspace_id, initial_context_id=context_id,
                                      holder_id="patrol", status="stopped", health="idle") for identity in (loop_id, other_loop)])
            await session.flush()
            session.add(DesktopRun(run_id=run_id, task_id=context_id, loop_id=loop_id, agent_id="main", kind="main", status="success"))
        projector = FactProjector(sessions, object())
        app = FastAPI()
        app.include_router(projection_router)
        app.state.desktop_service = SimpleNamespace(session_factory=sessions)
        app.state.agent_loop_fact_projector = projector
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield SimpleNamespace(sessions=sessions, projector=projector, client=client, app=app,
                                  loop_id=loop_id, other_loop=other_loop, context_id=context_id, run_id=run_id,
                                  url=f"/desktop/api/agent-loops/{loop_id}/projection-failures")
    finally:
        await engine.dispose()


async def record(env, *, unit_kind="run_backfill", unit_id=None, error=None, projector_name="loop_facts"):
    async with env.sessions.begin() as session:
        row = await ProjectionRecoveryRepository().record_failure(
            session, loop_id=env.loop_id, projector_name=projector_name,
            unit_kind=unit_kind, unit_id=unit_id or env.run_id, error=error or ValueError("private raw payload"),
        )
        return row.failure_id


def test_journal_http_repair_keeps_latest_fact_and_cursor_and_does_not_rerun(tmp_path):
    async def run():
        async with environment(tmp_path) as env:
            async with env.sessions.begin() as session:
                events = []
                for revision, status in ((1, "failed"), (2, "success")):
                    events.append(await LoopEventJournal().append(session, env.loop_id, CanonicalEventDraft(
                        kind="context.test.completed", entity_type="test_result", entity_id="one-result",
                        entity_revision=revision, idempotency_key=f"repair-{revision}",
                        payload={"context_id": env.context_id, "status": status, "summary": status},
                    )))
            original = env.projector._event_materializer.materialize

            async def poison(session, event):
                if event.event_id == events[0].event_id:
                    raise ValueError("private raw payload")
                return await original(session, event)

            env.projector._event_materializer.materialize = poison
            await env.projector.project_loop(env.loop_id)
            page = (await env.client.get(env.url)).json()
            failure_id = page["items"][0]["failure_id"]
            assert "private raw payload" not in str(page)
            assert page["items"][0]["can_repair"] is True
            url = f"{env.url}/{failure_id}/repair"

            # 物化后抛错必须回滚事实，且保留可再次显式修复的隔离状态。
            async def rollback(session, event):
                value = await original(session, event)
                if event.event_id == events[0].event_id:
                    raise TimeoutError("still broken")
                return value

            env.projector._event_materializer.materialize = rollback
            failed = (await env.client.post(url)).json()["failure"]
            assert failed["status"] == "quarantined" and failed["attempt_count"] == 2
            assert failed["error_class"] == "TimeoutError"
            async with env.sessions() as session:
                fact = await session.scalar(select(LoopFact).where(LoopFact.loop_id == env.loop_id))
                assert fact.presentation["outcome_status"] == "success"
            env.projector._event_materializer.materialize = original
            replies = await asyncio.gather(env.client.post(url), env.client.post(url))
            assert all(reply.status_code == 200 and reply.json()["failure"]["status"] == "resolved" for reply in replies)
            assert (await env.client.get(env.url)).json()["items"] == []
            assert (await env.client.get(env.url, params={"include_resolved": True})).json()["items"][0]["status"] == "resolved"
            async with env.sessions() as session:
                fact = await session.scalar(select(LoopFact).where(LoopFact.loop_id == env.loop_id))
                assert fact.presentation["outcome_status"] == "success"
                cursor = await session.get(LoopProjectorCursor, (env.loop_id, FactProjector.PROJECTOR_NAME))
                assert cursor.last_sequence == events[-1].sequence
                revision_count = await session.scalar(select(func.count()).select_from(LoopFactRevision).where(LoopFactRevision.loop_id == env.loop_id))
                outcome_count = await session.scalar(select(func.count()).select_from(LoopProjectionUnitOutcome).where(LoopProjectionUnitOutcome.loop_id == env.loop_id))
                source_run = await session.get(DesktopRun, env.run_id)
                assert source_run.status == "success"
                assert await session.scalar(select(func.count()).select_from(DesktopRun).where(DesktopRun.loop_id == env.loop_id)) == 1
            assert (await env.client.post(url)).status_code == 200
            async with env.sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopFactRevision).where(LoopFactRevision.loop_id == env.loop_id)) == revision_count
                assert await session.scalar(select(func.count()).select_from(LoopProjectionUnitOutcome).where(LoopProjectionUnitOutcome.loop_id == env.loop_id)) == outcome_count
    asyncio.run(run())


@pytest.mark.parametrize("missing_source", [False, True])
def test_backfill_repair_records_failure_and_resolves_only_existing_source(tmp_path, missing_source):
    async def run():
        async with environment(tmp_path) as env:
            failure_id = await record(env, unit_id=uuid4().hex if missing_source else env.run_id)
            calls = []

            async def broken(session, source, event):
                calls.append(source.run_id)
                raise ValueError("broken backfill")

            env.projector._materializer.materialize_run = broken
            url = f"{env.url}/{failure_id}/repair"
            failed = await env.client.post(url)
            assert failed.status_code == 200
            assert failed.json()["failure"]["status"] == "quarantined"
            assert failed.json()["failure"]["attempt_count"] == 2
            assert failed.json()["failure"]["error_class"] == ("LookupError" if missing_source else "ValueError")
            if not missing_source:
                async def repaired(session, source, event):
                    calls.append(source.run_id)
                    assert source.status == "success" and event is None
                    return ()
                env.projector._materializer.materialize_run = repaired
                result = (await env.client.post(url)).json()
                assert result["failure"]["status"] == "resolved" and result["processed"] == 1
                assert len(calls) == 2
            else:
                assert calls == []
            async with env.sessions() as session:
                outcomes = tuple(await session.scalars(select(LoopProjectionUnitOutcome).where(LoopProjectionUnitOutcome.loop_id == env.loop_id).order_by(LoopProjectionUnitOutcome.attempt)))
                assert [row.status for row in outcomes] == (["quarantined", "quarantined"] if missing_source else ["quarantined", "quarantined", "succeeded"])
    asyncio.run(run())


def test_repair_routes_enforce_loop_scope_state_availability_and_bounded_reads(tmp_path):
    async def run():
        async with environment(tmp_path) as env:
            failure_id = await record(env)
            retryable = await record(env, unit_id="temporary", error=TimeoutError("private"))
            unsupported = await record(env, unit_id="other", projector_name="another_projector")
            assert (await env.client.post(f"/desktop/api/agent-loops/{env.other_loop}/projection-failures/{failure_id}/repair")).status_code == 404
            with pytest.raises(LookupError):
                await env.projector.repair_failure(failure_id, loop_id=env.other_loop)
            for identity in (retryable, unsupported):
                assert (await env.client.post(f"{env.url}/{identity}/repair")).status_code == 409
            assert (await env.client.post(f"{env.url}/{uuid4().hex}/repair")).status_code == 404
            first = (await env.client.get(env.url, params={"limit": 1})).json()
            second = (await env.client.get(env.url, params={"limit": 2, "before": first["next_before"]})).json()
            assert first["has_more"] and not second["has_more"]
            assert {row["failure_id"] for row in first["items"] + second["items"]} == {failure_id, retryable, unsupported}
            assert (await env.client.get(env.url, params={"limit": 101})).status_code == 422
            assert (await env.client.get(f"/desktop/api/agent-loops/{uuid4().hex}/projection-failures")).status_code == 404
            env.app.state.agent_loop_fact_projector = None
            assert (await env.client.post(f"{env.url}/{failure_id}/repair")).status_code == 503
            async with env.sessions() as session:
                row = await session.get(LoopProjectionFailure, failure_id)
                assert row.status == "quarantined" and row.attempt_count == 1
    asyncio.run(run())


def test_repair_preserves_completed_verification_and_other_quarantined_units(tmp_path):
    async def run():
        async with environment(tmp_path) as env:
            fact_id = uuid4().hex
            async with env.sessions.begin() as session:
                session.add(LoopFact(fact_id=fact_id, loop_id=env.loop_id, identity_key=uuid4().hex * 2,
                                     fact_type="test", normalized_subject="verification", state="observed",
                                     occurred_at=datetime.now(UTC)))
                poison = await LoopEventJournal().append(session, env.loop_id, CanonicalEventDraft(
                    kind="test.poison", entity_type="test_unit", entity_id="poison", entity_revision=1,
                    payload={}, idempotency_key="poison"))
                for revision, target in ((1, "verifying"), (2, "verified")):
                    await LoopEventJournal().append(session, env.loop_id, CanonicalEventDraft(
                        kind="fact.verification.changed", entity_type="fact_verification", entity_id=fact_id,
                        entity_revision=revision, payload={"fact_id": fact_id, "target_state": target},
                        idempotency_key=f"verify-{revision}"))
                other = await LoopEventJournal().append(session, env.loop_id, CanonicalEventDraft(
                    kind="test.other_poison", entity_type="test_unit", entity_id="other", entity_revision=1,
                    payload={}, idempotency_key="other-poison"))
            original = env.projector._event_materializer.materialize
            async def poison_units(session, event):
                if event.event_id in {poison.event_id, other.event_id}:
                    raise ValueError("bad unit")
                return await original(session, event)
            env.projector._event_materializer.materialize = poison_units
            await env.projector.project_loop(env.loop_id)
            async with env.sessions() as session:
                problems = tuple(await session.scalars(select(LoopProjectionFailure).where(LoopProjectionFailure.loop_id == env.loop_id)))
                assert (await session.get(LoopFact, fact_id)).state == "verified", [(row.unit_id, row.error_message) for row in problems]
                row = await session.scalar(select(LoopProjectionFailure).where(LoopProjectionFailure.unit_id == poison.event_id))
                failure_id = row.failure_id
            env.projector._event_materializer.materialize = original
            reply = await env.client.post(f"{env.url}/{failure_id}/repair")
            assert reply.status_code == 200 and reply.json()["failure"]["status"] == "resolved"
            async with env.sessions() as session:
                assert (await session.get(LoopFact, fact_id)).state == "verified"
                remaining = await session.scalar(select(LoopProjectionFailure).where(LoopProjectionFailure.unit_id == other.event_id))
                assert remaining.status == "quarantined" and remaining.attempt_count == 1
    asyncio.run(run())


@pytest.mark.parametrize("unit_kind,canonical", [("journal_event", True), ("run_backfill", True), ("run_backfill", False)])
def test_old_run_repair_cannot_retire_newer_same_subject_fact(tmp_path, unit_kind, canonical):
    async def run():
        async with environment(tmp_path) as env:
            newer_id = uuid4().hex
            old_time = datetime.now(UTC) - timedelta(minutes=2)
            async with env.sessions.begin() as session:
                older = await session.get(DesktopRun, env.run_id)
                older.created_at = old_time
                session.add(DesktopRun(run_id=newer_id, task_id=env.context_id, loop_id=env.loop_id,
                                       agent_id="main", kind="main", status="success", created_at=old_time + timedelta(minutes=1)))
            async def sources(session, runs):
                return [{"fact_id": source.run_id, "kind": "test", "status": "success" if source.run_id == newer_id else "failed",
                         "title": "test", "summary": source.run_id, "occurred_at": old_time.isoformat(),
                         "evidence": {"domain_source_id": source.run_id, "run_id": source.run_id}}
                        for source in runs]
            env.projector._materializer._sources.read = sources
            async with env.sessions.begin() as session:
                newer = await session.get(DesktopRun, newer_id)
                events = []
                if canonical:
                    for identity in (env.run_id, newer_id):
                        events.append(await LoopEventJournal().append(session, env.loop_id, CanonicalEventDraft(
                            kind="context.run.settled", entity_type="context_run", entity_id=identity,
                            entity_revision=1, payload={"run_id": identity, "context_id": env.context_id}, idempotency_key=identity)))
                    assert events[0].occurred_at == events[1].occurred_at
                await env.projector._materializer.materialize_run(session, newer, events[-1] if canonical else None)
                unit_id = events[0].event_id if unit_kind == "journal_event" else env.run_id
            failure_id = await record(env, unit_kind=unit_kind, unit_id=unit_id)
            reply = await env.client.post(f"{env.url}/{failure_id}/repair")
            assert reply.status_code == 200 and reply.json()["failure"]["status"] == "resolved"
            async with env.sessions() as session:
                facts = tuple(await session.scalars(select(LoopFact).where(LoopFact.loop_id == env.loop_id)))
                newer_fact = next(fact for fact in facts if fact.source_run_id == newer_id)
                old_fact = next(fact for fact in facts if fact.source_run_id == env.run_id)
                assert newer_fact.state == "verified" and old_fact.state == "contradicted"
                relationship = await session.scalar(select(LoopFactRelationship).where(LoopFactRelationship.loop_id == env.loop_id))
                assert relationship.source_fact_id == newer_fact.fact_id and relationship.target_fact_id == old_fact.fact_id
    asyncio.run(run())


def test_normal_projection_obeys_sequence_when_transaction_time_decreases(tmp_path):
    async def run():
        async with environment(tmp_path) as env:
            async with env.sessions.begin() as session:
                events = []
                for revision in (1, 2):
                    events.append(await LoopEventJournal().append(session, env.loop_id, CanonicalEventDraft(
                        kind="context.test.completed", entity_type="test_result", entity_id="same-test",
                        entity_revision=revision, payload={"context_id": env.context_id, "summary": str(revision), "status": "success"},
                        idempotency_key=f"ordered-{revision}")))
                from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
                later = await session.get(LoopJournalEvent, events[1].event_id)
                later.occurred_at = events[0].occurred_at - timedelta(minutes=1)
            await env.projector.project_loop(env.loop_id)
            async with env.sessions() as session:
                fact = await session.scalar(select(LoopFact).where(LoopFact.loop_id == env.loop_id))
                assert fact.presentation["summary"] == "2"
                assert fact.evidence[0]["metadata"]["projection_sequence"] == events[1].sequence
    asyncio.run(run())


def test_mixed_concurrent_repair_commits_failed_attempt_before_success(tmp_path):
    async def run():
        async with environment(tmp_path) as env:
            failure_id = await record(env)
            calls = 0
            async def materialize(session, source, event):
                nonlocal calls
                calls += 1
                if calls == 1:
                    await asyncio.sleep(0.03)
                    raise ValueError("first attempt failed")
                return ()
            env.projector._materializer.materialize_run = materialize
            replies = await asyncio.gather(*(env.client.post(f"{env.url}/{failure_id}/repair") for _ in range(2)))
            assert all(reply.status_code == 200 for reply in replies)
            async with env.sessions() as session:
                assert (await session.get(LoopProjectionFailure, failure_id)).status == "resolved"
                outcomes = tuple(await session.scalars(select(LoopProjectionUnitOutcome).where(LoopProjectionUnitOutcome.loop_id == env.loop_id).order_by(LoopProjectionUnitOutcome.attempt)))
                assert [row.status for row in outcomes] == ["quarantined", "quarantined", "succeeded"]
            assert calls == 2
    asyncio.run(run())


@pytest.mark.parametrize("wrong_loop", [False, True])
def test_missing_or_cross_loop_run_behind_journal_remains_quarantined(tmp_path, wrong_loop):
    async def run():
        async with environment(tmp_path) as env:
            async with env.sessions.begin() as session:
                event = await LoopEventJournal().append(session, env.loop_id, CanonicalEventDraft(
                    kind="context.run.settled", entity_type="context_run", entity_id=env.run_id if wrong_loop else uuid4().hex,
                    entity_revision=1, payload={}, idempotency_key="missing-run"))
                if wrong_loop:
                    source = await session.get(DesktopRun, env.run_id)
                    source.loop_id = env.other_loop
            failure_id = await record(env, unit_kind="journal_event", unit_id=event.event_id)
            reply = await env.client.post(f"{env.url}/{failure_id}/repair")
            assert reply.status_code == 200
            assert reply.json()["failure"]["status"] == "quarantined"
            assert reply.json()["failure"]["error_class"] == "LookupError"
    asyncio.run(run())
