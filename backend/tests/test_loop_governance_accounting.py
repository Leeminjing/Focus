"""本文件对外提供真实数据库的执行父链、共享预算与暂停后结算回归。

输入为隔离 PostgreSQL、新问候 Loop 与受理的三代执行；输出为完整归属、实时预算阻断、幂等消费与迟到结算不复活断言。
具体工作流为经真实 admission 验证来源、逐 attempt 预留和结算、主动暂停，再由 finalizer 补齐业务终态及用量；结构化索引使用生产模型包装器验证已报告失败与取消预留分开结算。
示例：python -m pytest backend/tests/test_loop_governance_accounting.py -q。
历史已执行轮缺 Decision 时显示精确诊断；新待观察轮保持未完成而不被误报，只读查询不补写冻结事实。
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from focus.runtime.runs.schemas import RunStatus

from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDelegationGrant, LoopRound, LoopDirective
from backend.app.desktop.agent_loop.accounting_query import LoopAccountingQuery
from backend.app.desktop.agent_loop.round_consequences import RoundConsequenceReader
from backend.app.desktop.models import DesktopRun, ModelAttemptAudit
from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy
from backend.app.desktop.run_orchestration.admission import RunAdmissionService
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.execution_attempts.usage_receipts import ModelUsageReceipts
from backend.app.desktop.execution_attempts.model import ModelAttemptJournal
from test_agent_loop_round_liveness import _seed_loop, _stop
from test_loop_mission_bootstrap import _seed_greeting_loop
from test_loop_execution_ownership import _seed_launching_directive, _admit_run

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_owned_descendants_reserve_live_budget_and_settle_after_pause(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="receipt", started_at=datetime.now(UTC))
            directive_id = await _seed_launching_directive(sessions, fixture, label="receipt")
            parent_id = await _admit_run(sessions, fixture, directive_id)
            run_ids = [parent_id]
            async with sessions.begin() as session:
                parent = await session.get(DesktopRun, parent_id)
                grant = await session.get(LoopDelegationGrant, fixture["snapshot"]["grant"]["grant_id"])
                grant.budgets = {**grant.budgets, "max_model_calls": 3}
                for _ in range(2):
                    child = DesktopRun(run_id=uuid.uuid4().hex, task_id=parent.task_id, agent_id=uuid.uuid4().hex,
                        kind="worker", status="pending", loop_id=parent.loop_id, round_id=parent.round_id,
                        directive_id=directive_id, parent_run_id=run_ids[-1], equipment={"permissions": ["read"], "access_mode": "read-only"})
                    await RunAdmissionService(RunOwnershipPolicy().admit).admit(session, child)
                    run_ids.append(child.run_id)
                round_row = await session.get(LoopRound, fixture["round_id"])
                round_row.status = "running"
                round_row.decision_id = (await session.get(LoopDirective, directive_id)).decision_id
                for index, run_id in enumerate(run_ids):
                    audit = ModelAttemptAudit(attempt_id=f"audit-{run_id}", run_id=run_id,
                        execution_thread_id=parent.execution_thread_id, checkpoint_ns=f"worker-{index}",
                        checkpoint_id="prepared", source_manifest={}, status="prepared", usage_accounting={})
                    session.add(audit)
                    await session.flush()
                    await ModelUsageReceipts().reserve(session, audit, input_tokens=100)
                blocked = ModelAttemptAudit(attempt_id="blocked-" + parent_id, run_id=parent_id,
                    execution_thread_id=parent.execution_thread_id, checkpoint_ns="", checkpoint_id="prepared", source_manifest={}, status="prepared")
                with pytest.raises(ValueError, match="预算已耗尽"):
                    await ModelUsageReceipts().reserve(session, blocked, input_tokens=100)
                consequences = await RoundConsequenceReader().read(session, round_row)
                assert not consequences.stable and consequences.counts["unsettled_runs"] == 3
                assert set(consequences.identities["unsettled_runs"]) == set(run_ids)
            journal = ModelAttemptJournal(sessions, None)
            for run_id in run_ids[:2]:
                await journal.settle(f"audit-{run_id}", "completed", {}, {"input_tokens": 40, "output_tokens": 10})
                await journal.settle(f"audit-{run_id}", "completed", {}, {"input_tokens": 40, "output_tokens": 10})
            await fixture["service"].control(fixture["loop_id"], "pause")
            await journal.settle(f"audit-{run_ids[-1]}", "cancelled", {}, None)
            checkpointer = SimpleNamespace(aget_tuple=lambda _config: asyncio.sleep(0, result=None))
            finalizer = RunLifecycleFinalizer(sessions, checkpointer)
            for run_id in run_ids:
                record = SimpleNamespace(run_id=run_id, status=RunStatus.success, error=None,
                    model_call_count=0, prompt_input_tokens=0, prompt_output_tokens=0, prompt_cache_hit_tokens=0)
                result = await finalizer.finalize(record)
                assert result.status == "interrupted"
                assert (await finalizer.finalize(record)).idempotent
                from backend.app.desktop.agent_loop.coordinator import LoopCoordinator

                async with sessions.begin() as session:
                    event = SimpleNamespace(run_id=run_id, event_id=result.event_id, payload={})
                    coordinator = LoopCoordinator(sessions)
                    await coordinator.handle_run_settled(event, session)
                    await coordinator.handle_run_settled(event, session)
            async with sessions() as session:
                usage = await session.get(LoopBudgetUsage, fixture["loop_id"])
                assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (3, 180, 20)
                for run_id in run_ids:
                    row = await session.get(DesktopRun, run_id)
                    assert row.status == "interrupted" and row.settled_at and row.model_call_count == 1
                directive = await session.get(LoopDirective, directive_id)
                assert directive.status == "cancelled" and directive.lifecycle_state == "cancelled"
                view = await LoopAccountingQuery().read(session, fixture["loop_id"])
                assert view["unknown_model_attempts"] == 1 and view["cleanup_pending_runs"] == 0
                assert view["consumption"]["actual"] == {"model_calls": 2, "input_tokens": 80, "output_tokens": 20}
                assert view["consumption"]["unreported_reservations"] == {"model_calls": 1, "input_tokens": 100, "output_tokens": 0}
                assert view["initial_evidence"]["round_id"] is None and view["completed_rounds"] == 0
                assert (await session.get(AgentLoop, fixture["loop_id"])).status == "paused"
                from backend.app.desktop.agent_loop.live_projection_projector import LoopLiveSnapshotProjector

                live = await LoopLiveSnapshotProjector().project(session, fixture["loop_id"])
                assert all(live.runs[run_id].state["status"] == "interrupted" for run_id in run_ids)
                assert live.loop.state["status"] == "paused"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_production_index_attempts_keep_reported_failure_and_cancelled_reservation_separate(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage
    from pydantic import BaseModel
    from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
    from backend.app.desktop.agent_loop.model_usage_owner import OwnedModelUsage
    from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
    from backend.app.desktop.agent_loop.expansion_resource_policy import resolve_expansion_resources
    from backend.app.desktop.agent_loop.context_expansion.index_model_budget import BudgetedIndexModel, IndexModelBudget
    from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
    from backend.tests.incremental_index_support import Checkpoints
    from backend.tests.config_helpers import app_config_for

    class Result(BaseModel):
        answer: str

    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        sent = asyncio.Event()
        calls = 0

        class Provider:
            async def ainvoke(self, messages, config):
                nonlocal calls
                calls += 1
                if calls == 1:
                    config["callbacks"][0].usage_metadata["receipt-test"] = {
                        "input_tokens": 40, "output_tokens": 10, "total_tokens": 50}
                    return AIMessage(content=json.dumps({"wrong_field": "invalid"}))
                sent.set()
                await asyncio.Event().wait()

        monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", lambda **kwargs: Provider())
        task = None
        try:
            fixture = await _seed_loop(sessions, tmp_path, label="index-receipt", started_at=datetime.now(UTC))
            await LoopObservationService(sessions, Checkpoints()).capture(fixture["loop_id"], fixture["round_id"])
            config = app_config_for("receipt-test", None)
            config.models[0].curation_output_method = "prompt_json"
            model = RoleBoundStructuredModel(config, "semantic_index_projector", max_attempts=2)
            wrapped = BudgetedIndexModel(model, IndexModelBudget(resolve_expansion_resources({}, 1),
                usage_receipts=OwnedModelUsage(sessions, fixture["loop_id"], fixture["round_id"], "round", fixture["round_id"])))
            task = asyncio.create_task(wrapped.invoke(Result, "Return an answer", {"frozen": True}))
            await asyncio.wait_for(sent.wait(), 5)
            await fixture["service"].control(fixture["loop_id"], "pause")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with sessions() as session:
                rows = tuple((await session.scalars(select(LoopIndexBudgetReservation).where(
                    LoopIndexBudgetReservation.loop_id == fixture["loop_id"]))).all())
                assert len(rows) == 2 and all(row.model_calls == 1 for row in rows)
                reported = next(row for row in rows if row.settled_at)
                unknown = next(row for row in rows if row.settled_at is None)
                assert reported.actual_usage["input_tokens"] == 40 and reported.actual_usage["output_tokens"] == 10
                assert unknown.actual_usage["receipt_state"] == "unknown"
                view = await LoopAccountingQuery().read(session, fixture["loop_id"])
                assert view["consumption"]["actual"] == {"model_calls": 1, "input_tokens": 40, "output_tokens": 10}
                assert view["unknown_model_attempts"] == 1
                assert view["consumption"]["unreported_reservations"]["model_calls"] == 1
                assert view["consumption"]["occupied"]["model_calls"] == 2
                from backend.app.desktop.agent_loop.live_projection_projector import LoopLiveSnapshotProjector

                live = await LoopLiveSnapshotProjector().project(session, fixture["loop_id"])
                assert live.accounting.state["accounting"] == view
                assert live.loop.state["status"] == "paused"
                assert (await session.get(AgentLoop, fixture["loop_id"])).status == "paused"
            assert [row["outcome"] for row in wrapped.last_attempt_records] == ["error", "cancelled"]
            assert all(row["usage_managed"] for row in wrapped.last_attempt_records)
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if fixture:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_baseline_audits_fill_missing_run_counters_without_claiming_task_ownership(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_greeting_loop(sessions, tmp_path)
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                initial_id = fixture["run_id"]
                initial = await session.get(DesktopRun, initial_id)
                for index in range(2):
                    session.add(ModelAttemptAudit(attempt_id=uuid.uuid4().hex, run_id=initial_id,
                        execution_thread_id=initial.execution_thread_id, checkpoint_ns="", checkpoint_id="baseline",
                        source_manifest={}, status="completed", usage={"input_tokens": 40, "output_tokens": 10}))
                from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
                session.add(LoopIndexBudgetReservation(reservation_id=uuid.uuid4().hex, loop_id=loop.loop_id,
                    grant_revision=loop.authority_revision, model_calls=1, input_tokens=100, output_tokens=10,
                    actual_usage=None))
            async with sessions() as session:
                first = await LoopAccountingQuery().read(session, fixture["loop_id"])
                assert first == await LoopAccountingQuery().read(session, fixture["loop_id"])
                assert first["initial_evidence"]["model_calls"] == 2
                assert first["initial_evidence"]["input_tokens"] == 80
                assert first["initial_evidence"]["output_tokens"] == 20
                assert first["initial_evidence"]["round_id"] is None
                assert first["unknown_model_attempts"] == 1
                assert first["consumption"]["actual"]["model_calls"] == 0
                assert first["historical_reconciliation"]["owned_completed_attempts"] == 0
        finally:
            if fixture:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


def test_historical_executed_round_without_decision_is_diagnosed_without_rewriting(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="history", started_at=datetime.now(UTC))
        try:
            async with sessions.begin() as session:
                row = await session.get(LoopRound, fixture["snapshot"]["current_round_id"])
                first = await LoopAccountingQuery().read(session, fixture["loop_id"])
                assert first["round_history"][0]["diagnostics"] == []
                row.status = "superseded"
                session.add(DesktopRun(run_id=uuid.uuid4().hex, task_id=fixture["context_id"], agent_id="legacy", kind="main",
                    loop_id=fixture["loop_id"], round_id=row.round_id, status="success", settled_at=datetime.now(UTC)))
            async with sessions() as session:
                first = await LoopAccountingQuery().read(session, fixture["loop_id"])
                assert first == await LoopAccountingQuery().read(session, fixture["loop_id"])
                assert first["completed_rounds"] == 0
                assert first["round_history"][0]["diagnostics"] == ["missing_patrol_decision"]
                row = await session.get(LoopRound, fixture["snapshot"]["current_round_id"])
                assert row.status == "superseded" and row.observation_id is None and row.decision_id is None
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
