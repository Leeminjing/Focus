r"""本文件对外提供 Loop 交互、持久化与恢复缺陷的回归测试。

输入为含 NUL 的嵌套载荷、WaitRequest factory、Loop 激活候选与投影失败；输出为安全载荷、类型化等待、
最新直接用户 Run 决策和局部失败结果。具体工作流为先验证纯领域合同，再用隔离 PostgreSQL 验证事务边界。
示例：`pytest backend/tests/test_loop_resilience_regressions.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop.activation_eligibility import LoopActivationEligibilityResolver
from backend.app.desktop.agent_loop.activation_models import LoopActivation
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.fact_projector import FactProjector
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.agent_loop.journal_models import LoopProjectorCursor
from backend.app.desktop.agent_loop.live_projection_contract import LoopLiveProjection
from backend.app.desktop.agent_loop.live_snapshot_overlay import LoopLiveProjectionOverlay
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopRound
from backend.app.desktop.agent_loop.persistence_safety import PersistencePayloadNormalizer
from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure, LoopProjectionUnitOutcome
from backend.app.desktop.agent_loop.projection_recovery import ProjectionRecoveryRepository
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest, LoopWaitResponse
from backend.app.desktop.agent_loop.wait_requests import LoopWaitRequestFactory, LoopWaitRequestService, WaitRequestConflict
from backend.app.desktop.agent_loop.schemas import LoopCreateRequest, LoopWaitResponseRequest
from backend.app.desktop.agent_loop.service import AgentLoopService
from backend.app.desktop.context_curation.models import CurationLane, CurationProgram
from backend.app.desktop.context_evolution import ContextRevisionContract, ContextRevisionOriginKind, ContextRevisionPayloadMode, ContextRevisionProjectionStatus, ContextRevisionRef, ContextRevisionRepository
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.models import RunDispatch
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_persistence_normalizer_escapes_nested_nul_with_provenance() -> None:
    result = PersistencePayloadNormalizer.normalize(
        {"summary": "before\x00after", "nested": [{"value": "ok"}]},
        source="tool-message",
    )
    assert result.value == {"summary": r"before\u0000after", "nested": [{"value": "ok"}]}
    assert result.affected_paths == ("$.summary",)
    assert result.replacement_count == 1
    assert len(result.original_digest) == 64
    assert result.metadata()["normalized"] is True


def test_persistence_normalizer_preserves_unicode_leaves_and_stable_digest() -> None:
    source = {"普通": [1, True, None, "文本", "bad\ud800value"]}
    first = PersistencePayloadNormalizer.normalize(source, source="workspace-result")
    second = PersistencePayloadNormalizer.normalize(source, source="workspace-result")
    assert first.value == {"普通": [1, True, None, "文本", r"bad\ud800value"]}
    assert first.affected_paths == ("$.普通[4]",)
    assert first.original_digest == second.original_digest
    assert first.replacement_count == 1


def test_wait_request_factories_define_typed_response_contracts() -> None:
    clarification = LoopWaitRequestFactory.clarification("What target?", scope={"kind": "portfolio"})
    budget = LoopWaitRequestFactory.budget_action("input_tokens_budget", {"max_input_tokens": 10})
    recovery = LoopWaitRequestFactory.legacy_recovery("Old failure")
    assert clarification.response_mode == "text"
    assert clarification.response_contract == {"min_length": 1, "max_length": 12000}
    assert budget.response_mode == "action"
    assert {item["action"] for item in budget.response_contract["actions"]} == {"revise_budget", "stop"}
    assert recovery.kind == "legacy_recovery"
    assert {item["action"] for item in recovery.response_contract["actions"]} >= {"retry", "stop"}


def test_fact_reconciliation_is_supervised_without_blocking_startup() -> None:
    async def run() -> None:
        projector = FactProjector(None, object())
        blocked = asyncio.Event()

        async def reconcile_background() -> None:
            await blocked.wait()

        projector._reconcile_background = reconcile_background
        await asyncio.wait_for(projector.start(), timeout=0.1)
        assert projector._reconcile_task is not None
        assert projector._reconcile_task.done() is False
        await projector.close()

    asyncio.run(run())


def test_postgres_wait_journal_activation_and_projection_recovery(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        context_id = uuid.uuid4().hex
        loop_id = uuid.uuid4().hex
        first_run_id = "1" * 32
        newest_run_id = "2" * 32
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="resilience"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="resilience"))
                await session.flush()
                session.add_all([
                    DesktopRun(run_id=first_run_id, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", input_messages=[{"role": "user", "content": "old"}], created_at=datetime.now(UTC) - timedelta(minutes=1)),
                    DesktopRun(run_id=newest_run_id, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", input_messages=[{"role": "user", "content": "new"}], created_at=datetime.now(UTC)),
                ])
                session.add(AgentLoop(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, holder_id="patrol", status="running", health="observing"))

            resolver = LoopActivationEligibilityResolver()
            async with sessions() as session:
                eligibility = await resolver.resolve(session, context_id)
            assert eligibility.candidate_run_id == newest_run_id
            assert eligibility.reason == "nonterminal_predecessor"

            waits = LoopWaitRequestService()
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id, with_for_update=True)
                opened = await waits.open(session, loop, LoopWaitRequestFactory.clarification("值\x00需要确认"), created_by="test", correlation_id="wait-correlation")
                repeated = await waits.open(session, loop, LoopWaitRequestFactory.clarification("ignored"), created_by="test", correlation_id="ignored")
                assert repeated.request_id == opened.request_id
                request_id = opened.request_id

            async with sessions.begin() as session:
                request, response, created = await waits.resolve(session, request_id, answer={"text": "答\x00案"}, actor_id="user", request_revision=1, idempotency_key="response-key")
                assert created is True
                assert request.status == "resolved"
                response_id = response.response_id
            async with sessions.begin() as session:
                request, response, created = await waits.resolve(session, request_id, answer={"text": "答\x00案"}, actor_id="user", request_revision=1, idempotency_key="response-key")
                assert created is False
                assert response.response_id == response_id
            async with sessions.begin() as session:
                with pytest.raises(WaitRequestConflict):
                    await waits.resolve(session, request_id, answer={"text": "different"}, actor_id="user", request_revision=1, idempotency_key="another-key")

            journal = LoopEventJournal()
            async with sessions.begin() as session:
                event = await journal.append(session, loop_id, CanonicalEventDraft(kind="context.tool.completed", entity_type="tool", entity_id="tool-message-nul", entity_revision=1, payload={"tool_name": "wsl", "summary": "WSL\x00payload", "tool": {"output": "WSL\x00payload"}, "run_id": newest_run_id, "context_id": context_id}, idempotency_key="nul-tool-fixture"))
                assert event.payload["tool"]["output"] == r"WSL\u0000payload"
                assert event.payload["run_id"] == newest_run_id
                assert event.payload["_persistence_safety"]["replacement_count"] == 2
            async with sessions() as session:
                replay = await journal.read(session, loop_id, after_sequence=0)
                assert any(item.idempotency_key == "nul-tool-fixture" for item in replay)
            projector = FactProjector(sessions, object())
            assert await projector.project_loop(loop_id) >= 1
            async with sessions() as session:
                fact = await session.scalar(select(LoopFact).where(LoopFact.loop_id == loop_id, LoopFact.fact_type == "tool"))
                assert fact.source_run_id == newest_run_id
                assert fact.presentation["summary"] == r"WSL\u0000payload"
                assert fact.presentation["persistence_safety"]["normalized"] is True

            recovery = ProjectionRecoveryRepository()
            async with sessions.begin() as session:
                first = await recovery.record_failure(session, loop_id=loop_id, projector_name="loop_facts", unit_kind="journal_event", unit_id="poison", error=ValueError("bad\x00event"), boundary_sequence=7)
                failure_id = first.failure_id
            async with sessions.begin() as session:
                second = await recovery.record_failure(session, loop_id=loop_id, projector_name="loop_facts", unit_kind="journal_event", unit_id="poison", error=ValueError("bad\x00event"), boundary_sequence=7)
                assert second.failure_id == failure_id
                assert second.status == "quarantined"
                transient = await recovery.record_failure(session, loop_id=loop_id, projector_name="loop_facts", unit_kind="journal_event", unit_id="transient", error=TimeoutError("later"), boundary_sequence=8)
                assert transient.status == "retryable"
                assert transient.retryable is True
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopProjectionFailure).where(LoopProjectionFailure.unit_id == "poison")) == 1
                assert await session.scalar(select(func.count()).select_from(LoopProjectionUnitOutcome).where(LoopProjectionUnitOutcome.unit_id == "poison")) == 2
                assert await session.scalar(select(func.count()).select_from(LoopWaitRequest).where(LoopWaitRequest.loop_id == loop_id)) == 1
                assert await session.scalar(select(func.count()).select_from(LoopWaitResponse).where(LoopWaitResponse.request_id == request_id)) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_migration_backfills_legacy_wait_and_classifies_runs(tmp_path: Path) -> None:
    migrations = Path(__file__).parents[1] / "packages" / "harness" / "focus" / "persistence" / "migrations" / "alembic.ini"
    config = Config(str(migrations))
    engine = create_engine(make_url(os.environ["FOCUS_DATABASE_URL"]).set(drivername="postgresql+psycopg"))
    workspace_id = uuid.uuid4().hex
    waiting_context = uuid.uuid4().hex
    running_context = uuid.uuid4().hex
    loop_id = uuid.uuid4().hex
    pending_run_id = uuid.uuid4().hex
    running_run_id = uuid.uuid4().hex
    try:
        command.downgrade(config, "0a1b2c3d4e5f")
        with Session(engine) as session, session.begin():
            session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="legacy"))
            session.flush()
            session.add_all([
                DesktopThread(task_id=waiting_context, workspace_id=workspace_id, thread_id=f"thread-{waiting_context}", title="waiting"),
                DesktopThread(task_id=running_context, workspace_id=workspace_id, thread_id=f"thread-{running_context}", title="running"),
            ])
            session.flush()
            session.add(AgentLoop(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=waiting_context, holder_id="patrol", status="waiting_user", health="idle", waiting_reason="旧原因"))
            session.add_all([
                DesktopRun(run_id=pending_run_id, task_id=waiting_context, agent_id=f"main:{waiting_context}", kind="main", status="pending", origin="direct_user", execution_thread_id=f"thread-{waiting_context}", input_messages=[{"role": "user", "content": "你好"}]),
                DesktopRun(run_id=running_run_id, task_id=running_context, agent_id=f"main:{running_context}", kind="main", status="running", origin="direct_user", execution_thread_id=f"thread-{running_context}", input_messages=[{"role": "user", "content": "仍在执行"}]),
            ])
        command.upgrade(config, "head")
        with Session(engine) as session:
            wait = session.scalar(select(LoopWaitRequest).where(LoopWaitRequest.loop_id == loop_id))
            pending_dispatch = session.scalar(select(RunDispatch).where(RunDispatch.run_id == pending_run_id))
            running_dispatch = session.scalar(select(RunDispatch).where(RunDispatch.run_id == running_run_id))
            interrupted = session.get(DesktopRun, running_run_id)
            assert wait.kind == "legacy_recovery"
            assert wait.prompt == "旧原因"
            assert wait.status == "open"
            assert pending_dispatch.status == "interrupted"
            assert running_dispatch.status == "interrupted"
            assert session.get(DesktopRun, pending_run_id).input_messages[0]["content"] == "你好"
            assert interrupted.status == "interrupted"
            assert interrupted.input_messages[0]["content"] == "仍在执行"
    finally:
        command.upgrade(config, "head")
        engine.dispose()


def test_projector_quarantines_one_unit_continues_and_repairs(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        context_id = uuid.uuid4().hex
        healthy_context_id = uuid.uuid4().hex
        loop_id = uuid.uuid4().hex
        healthy_loop_id = uuid.uuid4().hex
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="projection"))
                await session.flush()
                session.add_all([
                    DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="degraded"),
                    DesktopThread(task_id=healthy_context_id, workspace_id=workspace_id, thread_id=f"thread-{healthy_context_id}", title="healthy"),
                ])
                await session.flush()
                session.add_all([
                    AgentLoop(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, holder_id="patrol", status="running", health="observing"),
                    AgentLoop(loop_id=healthy_loop_id, workspace_id=workspace_id, initial_context_id=healthy_context_id, holder_id="patrol", status="running", health="observing"),
                ])
            journal = LoopEventJournal()
            async with sessions.begin() as session:
                poison = await journal.append(session, loop_id, CanonicalEventDraft(kind="test.poison", entity_type="test_unit", entity_id="poison", entity_revision=1, payload={}, idempotency_key="projection-poison"))
                later = await journal.append(session, loop_id, CanonicalEventDraft(kind="test.good", entity_type="test_unit", entity_id="good", entity_revision=1, payload={}, idempotency_key="projection-good"))

            projector = FactProjector(sessions, object())
            original = projector._event_materializer.materialize

            async def fail_poison(session, event):
                if event.event_id == poison.event_id:
                    raise ValueError("deterministic poison")
                return await original(session, event)

            projector._event_materializer.materialize = fail_poison
            assert await projector.project_loop(loop_id) == 0
            async with sessions.begin() as session:
                cursor = await session.get(LoopProjectorCursor, (loop_id, FactProjector.PROJECTOR_NAME))
                failure = await session.scalar(select(LoopProjectionFailure).where(LoopProjectionFailure.unit_id == poison.event_id))
                degraded = await LoopLiveProjectionOverlay().apply(session, LoopLiveProjection(loop_id=loop_id), later.sequence)
                healthy = await LoopLiveProjectionOverlay().apply(session, LoopLiveProjection(loop_id=healthy_loop_id), 1)
                assert cursor.last_sequence == later.sequence
                assert failure.status == "quarantined"
                assert degraded.diagnostics.recovery_status == "degraded"
                assert degraded.diagnostics.quarantined_units == (poison.event_id,)
                assert healthy.diagnostics.recovery_status == "healthy"
                failure_id = failure.failure_id

            projector._event_materializer.materialize = original
            assert await projector.repair_failure(failure_id) == 0
            async with sessions() as session:
                repaired = await session.get(LoopProjectionFailure, failure_id)
                cursor = await session.get(LoopProjectorCursor, (loop_id, FactProjector.PROJECTOR_NAME))
                assert repaired.status == "resolved"
                assert cursor.last_sequence == later.sequence
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_activation_uses_newest_direct_run_and_terminal_predecessor_policy(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        context_id = uuid.uuid4().hex
        direct_old = uuid.uuid4().hex
        delegated_newer = uuid.uuid4().hex
        direct_new = uuid.uuid4().hex
        predecessor_id = uuid.uuid4().hex
        resolver = LoopActivationEligibilityResolver()
        try:
            base = datetime.now(UTC)
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="activation"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="activation"))
                await session.flush()
                session.add_all([
                    DesktopRun(run_id=direct_old, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", created_at=base - timedelta(minutes=3)),
                    DesktopRun(run_id=delegated_newer, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="delegated_patrol", created_at=base - timedelta(minutes=2)),
                    DesktopRun(run_id=direct_new, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="pending", origin="direct_user", created_at=base - timedelta(minutes=1)),
                    AgentLoop(loop_id=predecessor_id, workspace_id=workspace_id, initial_context_id=context_id, holder_id="patrol", status="stopped", health="idle", created_at=base),
                ])
            async with sessions() as session:
                eligible = await resolver.resolve(session, context_id)
            assert eligible.eligible is True
            assert eligible.candidate_run_id == direct_new
            assert eligible.predecessor_loop_id == predecessor_id
            readiness_token = eligible.consistency_token

            for status in ("running", "paused", "waiting_user"):
                async with sessions.begin() as session:
                    predecessor = await session.get(AgentLoop, predecessor_id, with_for_update=True)
                    predecessor.status = status
                async with sessions() as session:
                    blocked = await resolver.resolve(session, context_id)
                assert blocked.reason == "nonterminal_predecessor"

            for status in ("stopped", "completed", "failed"):
                async with sessions.begin() as session:
                    predecessor = await session.get(AgentLoop, predecessor_id, with_for_update=True)
                    predecessor.status = status
                async with sessions() as session:
                    terminal = await resolver.resolve(session, context_id)
                assert terminal.eligible is True

            async with sessions.begin() as session:
                predecessor = await session.get(AgentLoop, predecessor_id, with_for_update=True)
                predecessor.status = "failed"
                newest = await session.get(DesktopRun, direct_new, with_for_update=True)
                newest.loop_id = predecessor_id
            async with sessions() as session:
                bound = await resolver.resolve(session, context_id)
            assert bound.reason == "newest_run_already_bound"
            assert bound.candidate_run_id == direct_new

            newest_direct = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(DesktopRun(run_id=newest_direct, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="pending", origin="direct_user", created_at=base + timedelta(minutes=1)))
            async with sessions() as session:
                refreshed = await resolver.resolve(session, context_id)
            assert refreshed.candidate_run_id == newest_direct
            assert refreshed.consistency_token != readiness_token
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_wait_response_resumes_once_while_unrelated_task_admits(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        waiting_context = uuid.uuid4().hex
        other_context = uuid.uuid4().hex
        loop_id = uuid.uuid4().hex
        waits = LoopWaitRequestService()
        service = AgentLoopService(sessions)
        admission = RunAdmissionService()
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="wait-e2e"))
                await session.flush()
                session.add_all([
                    DesktopThread(task_id=waiting_context, workspace_id=workspace_id, thread_id=f"thread-{waiting_context}", title="waiting"),
                    DesktopThread(task_id=other_context, workspace_id=workspace_id, thread_id=f"thread-{other_context}", title="other"),
                ])
                await session.flush()
                loop = AgentLoop(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=waiting_context, holder_id="patrol", status="running", health="observing")
                session.add(loop)
                await session.flush()
                session.add(LoopDelegationGrant(grant_id=uuid.uuid4().hex, loop_id=loop_id, revision=1, holder_id="patrol", status="active", capabilities=[], context_scope=[], permission_scope=[], budgets={"max_input_tokens": 10}, delegable_gates=[]))
                request = await waits.open(session, loop, LoopWaitRequestFactory.retry_or_stop("需要用户选择"), created_by="recovery", correlation_id="wait-e2e")
                request_id = request.request_id

            assert await service.user_message(other_context, "另一个会话仍可发送") is None
            async with sessions.begin() as session:
                open_request = await waits.active(session, loop_id)
                assert open_request.request_id == request_id
                other_run = DesktopRun(run_id=uuid.uuid4().hex, task_id=other_context, agent_id=f"main:{other_context}", kind="main", status="pending", origin="direct_user", execution_thread_id=f"thread-{other_context}", idempotency_key="other-task-request", input_messages=[{"role": "user", "content": "另一个会话仍可发送"}], equipment={"_durable_dispatch_execution": {"agent_role": "main", "base_prompt": "test"}}, workspace_anchor={})
                admitted = await admission.admit(session, other_run)
                assert admitted.dispatch.status == "accepted"

            body = LoopWaitResponseRequest(request_revision=1, idempotency_key="wait-response-key", answer={"action": "retry"})
            first = await service.resolve_wait_request(loop_id, request_id, body, actor_id="user")
            repeated = await service.resolve_wait_request(loop_id, request_id, body, actor_id="user")
            assert first["created"] is True
            assert repeated["created"] is False
            assert first["loop_status"] == "running"

            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id, with_for_update=True)
                budget = await waits.open(session, loop, LoopWaitRequestFactory.budget_action("需要增加预算", {"max_input_tokens": 10}), created_by="budget", correlation_id="wait-budget")
                budget_request_id = budget.request_id
            revised = await service.resolve_wait_request(
                loop_id,
                budget_request_id,
                LoopWaitResponseRequest(request_revision=1, idempotency_key="wait-budget-response", answer={"action": "revise_budget", "budgets": {"max_input_tokens": 42}}),
                actor_id="user",
            )
            assert revised["loop_status"] == "running"
            async with sessions() as session:
                grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active"))
                assert grant.budgets["max_input_tokens"] == 42

            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id, with_for_update=True)
                stopping = await waits.open(session, loop, LoopWaitRequestFactory.retry_or_stop("是否停止"), created_by="recovery", correlation_id="wait-stop")
                stop_request_id = stopping.request_id
            stopped = await service.resolve_wait_request(
                loop_id,
                stop_request_id,
                LoopWaitResponseRequest(request_revision=1, idempotency_key="wait-stop-response", answer={"action": "stop"}),
                actor_id="user",
            )
            assert stopped["loop_status"] == "stopped"
            async with sessions() as session:
                rounds = tuple((await session.scalars(select(LoopRound).where(LoopRound.loop_id == loop_id))).all())
                replay = await LoopEventJournal().read(session, loop_id, after_sequence=0)
                assert len(rounds) == 1
                assert {item.kind for item in replay} >= {"loop.wait.request_opened", "loop.wait.response_committed", "loop.wait.request_resolved", "loop.round.observed", "loop.wait.resumed", "loop.wait.terminated"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_successor_retires_terminal_predecessor_lane_before_acquiring_context(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        context_id = uuid.uuid4().hex
        predecessor_id = uuid.uuid4().hex
        predecessor_program_id = uuid.uuid4().hex
        predecessor_lane_id = uuid.uuid4().hex
        run_id = uuid.uuid4().hex
        revision_id = uuid.uuid4().hex
        service = AgentLoopService(sessions)
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="successor-ownership"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="successor-ownership"))
                await session.flush()
                ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=f"thread-{context_id}", checkpoint_ns="", checkpoint_id=f"checkpoint-{context_id}", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
                await ContextRevisionRepository().insert(session, ContextRevisionContract(ref=ref, content_hash="b" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.RUN_SETTLED, origin_id=run_id, created_at=datetime.now(UTC)))
                await ContextRevisionRepository().switch_current(session, ref, None)
                session.add_all([
                    DesktopRun(run_id=run_id, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{context_id}", context_revision_id=revision_id, final_checkpoint_id=ref.checkpoint_id, settled_at=datetime.now(UTC)),
                    CurationProgram(program_id=predecessor_program_id, workspace_id=workspace_id, control_state="stopped", policy={"owner_loop_id": predecessor_id}, revision=1),
                    WorkspaceSlot(slot_id=uuid.uuid4().hex, workspace_id=workspace_id, kind="authoritative", root_path=str(tmp_path), provider="local", current_fingerprint="c" * 64, revision=1),
                ])
                await session.flush()
                session.add_all([
                    AgentLoop(loop_id=predecessor_id, workspace_id=workspace_id, initial_context_id=context_id, program_id=predecessor_program_id, holder_id="patrol", status="stopped", health="idle", completed_at=datetime.now(UTC)),
                    CurationLane(lane_id=predecessor_lane_id, program_id=predecessor_program_id, managed_context_id=context_id, purpose="Primary execution", normalized_purpose="primary execution", lane_policy={}, lifecycle="active", publisher_epoch=1, current_source_frontier_hash="d" * 64, current_semantic_fingerprint="b" * 64),
                ])
            async with sessions() as session:
                readiness = await LoopActivationEligibilityResolver().resolve(session, context_id)
            successor_id = uuid.uuid4().hex
            snapshot = await service.start(LoopCreateRequest(
                loop_id=successor_id,
                workspace_id=workspace_id,
                initial_context_id=context_id,
                initial_run_id=run_id,
                readiness_token=readiness.consistency_token,
                activation_key=f"successor:{run_id}",
                holder_id=f"patrol:{successor_id}",
                goal="Finish successor work",
                task_contract="Keep history",
                acceptance_criteria=({"criterion_id": "done", "text": "done"},),
                capabilities=("request_completion",),
                context_scope=(context_id,),
                permission_scope=("read",),
            ))
            assert snapshot["loop_id"] == successor_id
            async with sessions() as session:
                predecessor_lane = await session.get(CurationLane, predecessor_lane_id)
                live_lanes = tuple((await session.scalars(select(CurationLane).where(CurationLane.managed_context_id == context_id, CurationLane.lifecycle != "retired"))).all())
                assert predecessor_lane.lifecycle == "retired"
                assert len(live_lanes) == 1
                assert live_lanes[0].program_id == snapshot["program_id"]
        finally:
            current = await service.active_for_context(context_id)
            if current is not None:
                await service.control(current["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


def test_successor_reports_structured_nonterminal_lane_owner_conflict(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        context_id = uuid.uuid4().hex
        owner_id = uuid.uuid4().hex
        program_id = uuid.uuid4().hex
        lane_id = uuid.uuid4().hex
        run_id = uuid.uuid4().hex
        revision_id = uuid.uuid4().hex
        service = AgentLoopService(sessions)
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="ownership-conflict"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="ownership-conflict"))
                await session.flush()
                ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=f"thread-{context_id}", checkpoint_ns="", checkpoint_id=f"checkpoint-{context_id}", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
                await ContextRevisionRepository().insert(session, ContextRevisionContract(ref=ref, content_hash="e" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.RUN_SETTLED, origin_id=run_id, created_at=datetime.now(UTC)))
                await ContextRevisionRepository().switch_current(session, ref, None)
                session.add_all([
                    DesktopRun(run_id=run_id, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{context_id}", context_revision_id=revision_id, final_checkpoint_id=ref.checkpoint_id, settled_at=datetime.now(UTC)),
                    CurationProgram(program_id=program_id, workspace_id=workspace_id, policy={"owner_loop_id": owner_id}, revision=1),
                    WorkspaceSlot(slot_id=uuid.uuid4().hex, workspace_id=workspace_id, kind="authoritative", root_path=str(tmp_path), provider="local", current_fingerprint="f" * 64, revision=1),
                ])
                await session.flush()
                session.add_all([
                    AgentLoop(loop_id=owner_id, workspace_id=workspace_id, initial_context_id=context_id, program_id=program_id, holder_id="patrol", status="waiting_user", health="idle"),
                    CurationLane(lane_id=lane_id, program_id=program_id, managed_context_id=context_id, purpose="Primary execution", normalized_purpose="primary execution", lane_policy={}),
                ])
            async with sessions() as session:
                readiness = await LoopActivationEligibilityResolver().resolve(session, context_id)
            with pytest.raises(HTTPException) as captured:
                await service.start(LoopCreateRequest(
                    loop_id=uuid.uuid4().hex,
                    workspace_id=workspace_id,
                    initial_context_id=context_id,
                    initial_run_id=run_id,
                    readiness_token=readiness.consistency_token,
                    activation_key=f"conflict:{run_id}",
                    holder_id="patrol:new",
                    goal="Should conflict",
                    task_contract="Keep owner",
                    acceptance_criteria=({"criterion_id": "done", "text": "done"},),
                    capabilities=("request_completion",),
                    context_scope=(context_id,),
                    permission_scope=("read",),
                ))
            assert captured.value.status_code == 409
            assert captured.value.detail["code"] == "curation_ownership_conflict"
            assert captured.value.detail["context_id"] == context_id
            assert captured.value.detail["owner_loop_id"] == owner_id
            assert captured.value.detail["lane_id"] == lane_id
            assert "uq_curation_lane" not in str(captured.value.detail)
            assert "INSERT" not in str(captured.value.detail)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_equivalent_loop_authorization_creates_one_successor(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        context_id = uuid.uuid4().hex
        run_id = uuid.uuid4().hex
        revision_id = uuid.uuid4().hex
        service = AgentLoopService(sessions)
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="activation-race"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="activation-race"))
                await session.flush()
                ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=f"thread-{context_id}", checkpoint_ns="", checkpoint_id=f"checkpoint-{context_id}", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
                await ContextRevisionRepository().insert(session, ContextRevisionContract(ref=ref, content_hash="a" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.RUN_SETTLED, origin_id=run_id, created_at=datetime.now(UTC)))
                await ContextRevisionRepository().switch_current(session, ref, None)
                session.add(DesktopRun(run_id=run_id, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{context_id}", context_revision_id=revision_id, final_checkpoint_id=ref.checkpoint_id, settled_at=datetime.now(UTC)))
            async with sessions() as session:
                readiness = await LoopActivationEligibilityResolver().resolve(session, context_id)
            base = dict(workspace_id=workspace_id, initial_context_id=context_id, initial_run_id=run_id, readiness_token=readiness.consistency_token, activation_key="same-authorization", holder_id="patrol", goal="Finish", task_contract="Keep scope", acceptance_criteria=({"criterion_id": "done", "text": "done"},), capabilities=("request_completion",), context_scope=(context_id,), permission_scope=("read",))
            first, second = await asyncio.gather(
                service.start(LoopCreateRequest(loop_id=uuid.uuid4().hex, **base)),
                service.start(LoopCreateRequest(loop_id=uuid.uuid4().hex, **base)),
            )
            assert first["loop_id"] == second["loop_id"]
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopActivation).where(LoopActivation.selected_run_id == run_id)) == 1
                assert await session.scalar(select(func.count()).select_from(AgentLoop).where(AgentLoop.initial_context_id == context_id)) == 1
                bound = await session.get(DesktopRun, run_id)
                assert bound.loop_id == first["loop_id"]
        finally:
            current = await service.active_for_context(context_id)
            if current is not None:
                await service.control(current["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_non_equivalent_loop_authorization_reports_committed_winner(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id = uuid.uuid4().hex
        context_id = uuid.uuid4().hex
        run_id = uuid.uuid4().hex
        revision_id = uuid.uuid4().hex
        service = AgentLoopService(sessions)
        try:
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="activation-race-conflict"))
                await session.flush()
                session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{context_id}", title="activation-race-conflict"))
                await session.flush()
                ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=f"thread-{context_id}", checkpoint_ns="", checkpoint_id=f"checkpoint-{context_id}", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
                await ContextRevisionRepository().insert(session, ContextRevisionContract(ref=ref, content_hash="9" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.RUN_SETTLED, origin_id=run_id, created_at=datetime.now(UTC)))
                await ContextRevisionRepository().switch_current(session, ref, None)
                session.add(DesktopRun(run_id=run_id, task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{context_id}", context_revision_id=revision_id, final_checkpoint_id=ref.checkpoint_id, settled_at=datetime.now(UTC)))
            async with sessions() as session:
                readiness = await LoopActivationEligibilityResolver().resolve(session, context_id)
            base = dict(workspace_id=workspace_id, initial_context_id=context_id, initial_run_id=run_id, readiness_token=readiness.consistency_token, holder_id="patrol", goal="Finish", task_contract="Keep scope", acceptance_criteria=({"criterion_id": "done", "text": "done"},), capabilities=("request_completion",), context_scope=(context_id,), permission_scope=("read",))
            results = await asyncio.gather(
                service.start(LoopCreateRequest(loop_id=uuid.uuid4().hex, activation_key="authorization-a", **base)),
                service.start(LoopCreateRequest(loop_id=uuid.uuid4().hex, activation_key="authorization-b", **base)),
                return_exceptions=True,
            )
            snapshots = [item for item in results if isinstance(item, dict)]
            conflicts = [item for item in results if isinstance(item, HTTPException)]
            assert len(snapshots) == len(conflicts) == 1
            assert conflicts[0].status_code == 409
            assert conflicts[0].detail["code"] == "loop_activation_conflict"
            assert conflicts[0].detail["owner_loop_id"] == snapshots[0]["loop_id"]
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopActivation).where(LoopActivation.selected_run_id == run_id)) == 1
                assert await session.scalar(select(func.count()).select_from(CurationLane).where(CurationLane.managed_context_id == context_id, CurationLane.lifecycle != "retired")) == 1
        finally:
            current = await service.active_for_context(context_id)
            if current is not None:
                await service.control(current["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())
