"""本文件提供版本回退、真实进程崩溃恢复和组合故障的操作验收。

输入为隔离 PostgreSQL、旧 HEAD release、当前服务及确定性模型端口；输出为逐项状态断言和 JSON 演练证据。
工作流为冻结三项输入，启动独立消费者，在提交前后强杀进程，切换旧/新代码并恢复；
组合演练同时阻塞两种消费者，推进 Run/Test、修订 Mission、拒绝陈旧 Portfolio，再隔离修复并收口。
示例：设置 FOCUS_DRILL_CONTAINER 后执行 pytest backend/tests/test_round_memory_operational_drills.py。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from round_memory_drill_support import (
    ROOT,
    DrillProcess,
    backup_restore,
    extract_legacy_release,
    memory_snapshot,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_agent_loop_round_liveness import _seed_loop, _stop
from test_atomic_portfolio_publication import _CheckpointWriter, _compiled
from test_round_task_progress import _Checkpointer

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopDirective,
    LoopObservation,
)
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure
from backend.app.desktop.agent_loop.run_activity_writer import LoopRunActivityWriter
from backend.app.desktop.agent_loop.task_progress.contracts import canonical_hash
from backend.app.desktop.agent_loop.task_progress.models import (
    LoopDecisionInputs,
    LoopProgressHead,
    LoopProgressReceipt,
    LoopProgressWork,
    LoopTaskProgress,
)
from backend.app.desktop.agent_loop.task_progress.repository import ProgressNotReady
from backend.app.desktop.context_curation import (
    CurationProgramRepository,
    PortfolioCandidatePreparer,
    PortfolioFreezer,
    PortfolioFreezeRequest,
    PortfolioLaneAction,
    PortfolioLaneCandidate,
    PortfolioLaneIntent,
    PortfolioRevision,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionPublisher,
    ContextRevisionRepository,
)
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer

pytestmark = [
    pytest.mark.usefixtures("isolated_postgres_database"),
    pytest.mark.skipif(
        not os.environ.get("FOCUS_DRILL_CONTAINER"),
        reason="Operational drills require an explicitly owned PostgreSQL container",
    ),
]


@pytest.fixture(scope="module")
def legacy_release(tmp_path_factory):
    return extract_legacy_release(tmp_path_factory.getbasetemp() / "legacy-release")


async def _state(sessions, loop_id, observation_id):
    async with sessions() as session:
        head = await session.get(LoopProgressHead, loop_id)
        progress = await session.get(LoopTaskProgress, head.progress_id)
        work = await session.get(LoopProgressWork, observation_id)
        inputs = await session.get(LoopDecisionInputs, observation_id)
        versions = list(
            await session.scalars(
                select(LoopTaskProgress)
                .where(LoopTaskProgress.loop_id == loop_id)
                .order_by(LoopTaskProgress.generation)
            )
        )
        receipts = list(
            await session.scalars(
                select(LoopProgressReceipt.source_key).where(
                    LoopProgressReceipt.loop_id == loop_id
                )
            )
        )
        return {
            "generation": progress.generation,
            "document": progress.document,
            "head_hash": progress.content_hash,
            "input_hash": inputs.content_hash,
            "inputs": inputs.payload,
            "state": work.state,
            "fence": work.fence,
            "attempts": work.attempts,
            "usage": work.usage,
            "versions": [p.generation for p in versions],
            "receipts": receipts,
        }


async def _expire_claim(sessions, observation_id):
    async with sessions.begin() as session:
        work = await session.get(LoopProgressWork, observation_id, with_for_update=True)
        if work.state == "claimed":
            work.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)


async def _test_event(sessions, fixture, *, passed, failed, run_id=None):
    if run_id is None:
        async with sessions() as session:
            run_id = await session.scalar(
                select(DesktopRun.run_id)
                .where(DesktopRun.loop_id == fixture["loop_id"])
                .limit(1)
            )
    return await LoopRunActivityWriter(sessions, fixture["loop_id"]).write(
        CanonicalEventDraft(
            kind="context.test.completed",
            entity_type="test_result",
            entity_id=uuid.uuid4().hex,
            entity_revision=1,
            payload={
                "run_id": run_id,
                "context_id": fixture["context_id"],
                "status": "failed" if failed else "passed",
                "summary": "Typed test result",
                "metrics": {
                    "passed": passed,
                    "failed": failed,
                    "count_status": "exact",
                },
            },
            idempotency_key=uuid.uuid4().hex,
        )
    )


async def _run_child(
    processes, directory, mode, loop_id, *, release=ROOT, entity_id=None
):
    process = DrillProcess(release, directory, mode, loop_id, entity_id)
    processes.append(process)
    await process.finish()
    return process


@pytest.mark.parametrize(
    "phase,signal,generation",
    [
        ("hold-model", "model_started", 0),
        ("before-commit", "before_commit", 0),
        ("after-commit", "after_commit", 1),
    ],
)
def test_killed_process_legacy_rollback_and_new_release_resume(
    tmp_path, legacy_release, phase, signal, generation
):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        processes = []
        fixture = None
        try:
            fixture = await _seed_loop(
                sessions, tmp_path, label=phase, started_at=datetime.now(UTC)
            )
            loop_id = fixture["loop_id"]
            capture = LoopObservationService(sessions, _Checkpointer())
            envelope = await capture.capture(loop_id, fixture["round_id"])
            observation_id = envelope.decision_inputs_ref
            await fixture["service"].control(loop_id, "pause")
            late = await _test_event(sessions, fixture, passed=41, failed=2)
            crashed = DrillProcess(ROOT, tmp_path / "crashed", phase, loop_id)
            processes.append(crashed)
            await crashed.signal(signal)
            crashed.close()
            state = await _state(sessions, loop_id, observation_id)
            assert state["generation"] == generation
            assert state["state"] == ("published" if generation else "claimed")
            assert state["versions"] == list(range(generation + 1))
            frozen = state["input_hash"]
            before = await memory_snapshot(sessions, loop_id)
            legacy = await _run_child(
                processes,
                tmp_path / "legacy",
                "legacy-probe",
                loop_id,
                release=legacy_release,
            )
            legacy_result = await legacy.signal("legacy_read")
            assert legacy_result["status"] == "paused"
            assert legacy_release.resolve() in Path(legacy_result["origin"]).parents
            assert await memory_snapshot(sessions, loop_id) == before
            restored = (
                await backup_restore(sessions, loop_id, tmp_path)
                if phase == "after-commit"
                else None
            )
            await _expire_claim(sessions, observation_id)
            await _run_child(processes, tmp_path / "recovered", "publish", loop_id)
            recovered = await _state(sessions, loop_id, observation_id)
            assert recovered["generation"] == 1 and recovered["state"] == "published"
            assert recovered["versions"] == [0, 1] and recovered["input_hash"] == frozen
            assert recovered["fence"] == (1 if generation else 2)
            if phase == "hold-model":
                assert recovered["usage"][0]["usage_reported"] is False
            source_ids = {
                s["source_id"] for s in recovered["inputs"]["task_delta"]["sources"]
            }
            assert late.entity_id not in source_ids
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop_id)
                assert loop.status == "paused"
                assert not list(
                    await session.scalars(
                        select(LoopDirective).where(LoopDirective.loop_id == loop_id)
                    )
                )
            resumed = await fixture["service"].control(loop_id, "resume")
            successor = await capture.capture(loop_id, resumed["current_round_id"])
            assert late.entity_id in {
                s["source_id"] for s in successor.task_delta["sources"]
            }
            await _run_child(processes, tmp_path / "next-round", "publish", loop_id)
            final = await _state(sessions, loop_id, observation_id)
            assert final["versions"] == [0, 1, 2] and final["input_hash"] == frozen
            assert len(final["receipts"]) == len(set(final["receipts"]))
            (tmp_path / "evidence.json").write_text(
                json.dumps(
                    {
                        "scenario": phase,
                        "processes": [p.evidence for p in processes],
                        "state": final,
                        "backup_restore": restored,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        finally:
            for process in processes:
                process.close()
            if fixture:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(exercise())


async def _prepare_portfolio(sessions, fixture):
    revisions = ContextRevisionRepository()
    programs = CurationProgramRepository()
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, fixture["loop_id"])
        source = await revisions.current(session, fixture["context_id"])
        program = await programs.create(
            session, loop.workspace_id, program_id=uuid.uuid4().hex
        )
        lane = await programs.add_lane(
            session, program.program_id, "E2E", lane_id=uuid.uuid4().hex
        )
        request = PortfolioFreezeRequest(
            program_id=program.program_id,
            source_frontier=(source.ref,),
            lane_intents=(
                PortfolioLaneIntent(
                    lane_id=lane.lane_id,
                    action=PortfolioLaneAction.CREATE,
                    purpose=lane.purpose,
                    source_allocation=(source.ref,),
                    semantic_fingerprint="e" * 64,
                ),
            ),
            workspace_revision="workspace-r1",
            loop_revision=loop.revision,
            grant_revision=loop.authority_revision,
        )
    frozen = await PortfolioFreezer(sessions, revisions).freeze(request)
    compiled = _compiled(
        lane.lane_id, lane.purpose, source.ref, "E2E candidate"
    ).model_copy(update={"action": "create", "semantic_fingerprint": "e" * 64})
    await PortfolioCandidatePreparer(
        sessions, ContextRevisionPublisher(revisions, _CheckpointWriter()), revisions
    ).prepare(frozen.portfolio_revision_id, {frozen.candidate_ids[0]: compiled})
    return frozen


async def _start_run(sessions, fixture):
    run_id = uuid.uuid4().hex
    async with sessions.begin() as session:
        context = await session.get(DesktopThread, fixture["context_id"])
        session.add(
            DesktopRun(
                run_id=run_id,
                task_id=context.task_id,
                agent_id=f"main:{context.task_id}",
                kind="main",
                status="running",
                origin="direct_user",
                loop_id=fixture["loop_id"],
                execution_thread_id=context.thread_id,
                context_revision_id=context.current_revision_id,
            )
        )
    return run_id


async def _settle_run(sessions, run_id):
    record = SimpleNamespace(
        run_id=run_id,
        status=SimpleNamespace(value="error"),
        error="drill execution ended",
        model_call_count=0,
        prompt_input_tokens=0,
        prompt_output_tokens=0,
        prompt_cache_hit_tokens=0,
    )
    result = await RunLifecycleFinalizer(sessions, _Checkpointer()).finalize(record)
    assert result.status == "error"
    async with sessions() as session:
        assert (await session.get(DesktopRun, run_id)).settled_at is not None
    return run_id


async def _takeover(sessions, fixture, capture, run_id):
    loop_id = fixture["loop_id"]
    async with asyncio.timeout(5):
        await _settle_run(sessions, run_id)
        good = await _test_event(sessions, fixture, passed=43, failed=0, run_id=run_id)
        revised = await fixture["service"].override(
            loop_id,
            goal="Revised E2E delivery",
            task_contract="Use the revised acceptance",
            acceptance_criteria=[
                {"criterion_id": "tests", "text": "all 43 tests pass"}
            ],
        )
        with pytest.raises(ProgressNotReady):
            await capture.capture(loop_id, revised["current_round_id"])
        paused = await fixture["service"].control(loop_id, "pause")
    assert revised["goal_revision"] == 2 and paused["status"] == "paused"
    return good


async def _quarantine_and_continue(sessions, processes, tmp_path, loop_id, bad):
    for attempt in range(3):
        await _run_child(
            processes,
            tmp_path / f"failed-fact-{attempt}",
            "fail-fact",
            loop_id,
            entity_id=bad.event_id,
        )
        async with sessions() as session:
            failure = await session.scalar(
                select(LoopProjectionFailure).where(
                    LoopProjectionFailure.loop_id == loop_id,
                    LoopProjectionFailure.unit_id == bad.event_id,
                )
            )
            assert failure.status == ("quarantined" if attempt == 2 else "retryable")
            failure_id = failure.failure_id
    async with sessions() as session:
        tests = list(
            await session.scalars(
                select(LoopFact).where(
                    LoopFact.loop_id == loop_id, LoopFact.fact_type == "test"
                )
            )
        )
        assert len(tests) == 1 and tests[0].presentation["metrics"]["passed"] == 43
    return failure_id


async def _repair_projection(sessions, processes, tmp_path, loop_id, failure_id):
    await _run_child(
        processes,
        tmp_path / "repair-fact",
        "repair-fact",
        loop_id,
        entity_id=failure_id,
    )
    async with sessions() as session:
        tests = list(
            await session.scalars(
                select(LoopFact).where(
                    LoopFact.loop_id == loop_id, LoopFact.fact_type == "test"
                )
            )
        )
        assert len(tests) == 2
        assert (
            await session.get(LoopProjectionFailure, failure_id)
        ).status == "resolved"


async def _terminal_round(
    sessions, fixture, processes, tmp_path, next_envelope, run_id
):
    loop_id = fixture["loop_id"]
    terminal_memory = DrillProcess(
        ROOT, tmp_path / "terminal-memory", "hold-model", loop_id
    )
    processes.append(terminal_memory)
    await terminal_memory.signal("model_started")
    terminal_test = await _test_event(
        sessions, fixture, passed=44, failed=0, run_id=run_id
    )
    terminal_fact = DrillProcess(
        ROOT, tmp_path / "terminal-fact", "hold-fact", loop_id, terminal_test.event_id
    )
    processes.append(terminal_fact)
    await terminal_fact.signal("fact_blocked")
    async with asyncio.timeout(5):
        stopped = await fixture["service"].control(loop_id, "stop")
    assert stopped["status"] == "stopped"
    terminal_memory.close()
    terminal_fact.close()
    await _expire_claim(sessions, next_envelope.decision_inputs_ref)
    await _run_child(processes, tmp_path / "next-memory", "publish", loop_id)
    await _run_child(
        processes, tmp_path / "terminal-fact-restart", "project-facts", loop_id
    )


def test_combined_consumer_failures_mission_revision_superseded_and_restart(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        processes = []
        fixture = None
        try:
            fixture = await _seed_loop(
                sessions, tmp_path, label="combined-fault", started_at=datetime.now(UTC)
            )
            loop_id = fixture["loop_id"]
            portfolio = await _prepare_portfolio(sessions, fixture)
            run_id = await _start_run(sessions, fixture)
            bad = await _test_event(
                sessions, fixture, passed=41, failed=2, run_id=run_id
            )
            capture = LoopObservationService(sessions, _Checkpointer())
            envelope = await capture.capture(loop_id, fixture["round_id"])
            observation_id = envelope.decision_inputs_ref
            original = await _state(sessions, loop_id, observation_id)
            committed_publications = (await memory_snapshot(sessions, loop_id))[
                "publications"
            ]
            memory = DrillProcess(ROOT, tmp_path / "slow-memory", "hold-model", loop_id)
            processes.append(memory)
            await memory.signal("model_started")
            fact = DrillProcess(
                ROOT, tmp_path / "slow-fact", "hold-fact", loop_id, bad.event_id
            )
            processes.append(fact)
            await fact.signal("fact_blocked")
            good = await _takeover(sessions, fixture, capture, run_id)
            stale = await _run_child(
                processes,
                tmp_path / "stale-portfolio",
                "publish-portfolio",
                loop_id,
                entity_id=portfolio.portfolio_revision_id,
            )
            assert (await stale.signal("portfolio_result"))["outcome"] == "superseded"
            async with sessions() as session:
                publication = await session.get(
                    PortfolioRevision, portfolio.portfolio_revision_id
                )
                candidate = await session.get(
                    PortfolioLaneCandidate, portfolio.candidate_ids[0]
                )
                target = await session.get(DesktopThread, candidate.target_context_id)
                assert (
                    publication.status == "superseded"
                    and target.current_revision_id is None
                )
            assert (await memory_snapshot(sessions, loop_id))[
                "publications"
            ] == committed_publications
            memory.close()
            fact.close()
            await _expire_claim(sessions, observation_id)
            await _run_child(
                processes, tmp_path / "failed-memory", "fail-model", loop_id
            )
            failed = await _state(sessions, loop_id, observation_id)
            assert (
                failed["generation"] == 0
                and failed["state"] == "pending"
                and failed["attempts"] == 2
            )
            failure_id = await _quarantine_and_continue(
                sessions, processes, tmp_path, loop_id, bad
            )
            await _run_child(processes, tmp_path / "memory-restart", "publish", loop_id)
            published = await _state(sessions, loop_id, observation_id)
            assert (
                published["generation"] == 1
                and published["attempts"] == 3
                and published["document"]["mission_revision"] == 1
            )
            assert (
                published["input_hash"] == original["input_hash"]
                and published["inputs"] == original["inputs"]
            )
            async with sessions() as session:
                assert (await session.get(AgentLoop, loop_id)).status == "paused"
                assert (await session.get(LoopBudgetUsage, loop_id)).model_calls == 3
            await _repair_projection(sessions, processes, tmp_path, loop_id, failure_id)
            assert (await _state(sessions, loop_id, observation_id))[
                "head_hash"
            ] == published["head_hash"]
            resumed = await fixture["service"].control(loop_id, "resume")
            next_envelope = await capture.capture(loop_id, resumed["current_round_id"])
            assert good.entity_id in {
                s["source_id"] for s in next_envelope.task_delta["sources"]
            }
            await _terminal_round(
                sessions, fixture, processes, tmp_path, next_envelope, run_id
            )
            final = await _state(sessions, loop_id, observation_id)
            assert (
                final["generation"] == 2 and final["document"]["mission_revision"] == 2
            )
            async with sessions() as session:
                assert (await session.get(AgentLoop, loop_id)).status == "stopped"
                assert (await session.get(LoopBudgetUsage, loop_id)).model_calls == 5
                assert not list(
                    await session.scalars(
                        select(LoopDirective).where(LoopDirective.loop_id == loop_id)
                    )
                )
                stored = await session.get(LoopObservation, observation_id)
                assert canonical_hash(stored.envelope) == canonical_hash(
                    envelope.model_dump(mode="json")
                )
            (tmp_path / "evidence.json").write_text(
                json.dumps(
                    {
                        "scenario": "combined-fault",
                        "processes": [p.evidence for p in processes],
                        "run_id": run_id,
                        "failure_id": failure_id,
                        "portfolio_id": portfolio.portfolio_revision_id,
                        "original": original,
                        "final": final,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        finally:
            for process in processes:
                process.close()
            if fixture:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(exercise())
