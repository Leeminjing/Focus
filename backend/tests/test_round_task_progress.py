"""本文件对外提供冻结记忆与独立消费者的验收测试。

输入为纯记忆合同或隔离 PostgreSQL 的真实 Loop/来源；输出为时间边界和原子性断言。
具体工作流为冻结 N，提交迟到结果，领取/发布并重试，然后冻结 N+1；不调用外部模型。
示例：python -m pytest backend/tests/test_round_task_progress.py -q。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from focus.runtime.runs.usage import ModelUsage
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_agent_loop_round_liveness import _seed_loop, _stop

from backend.app.desktop.agent_loop.authority_control import LoopAuthorityService
from backend.app.desktop.agent_loop.decision_context import PatrolDecisionContext
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.agent_loop.fact_projector import FactProjector
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.models import (
    LoopBudgetUsage,
    LoopDirective,
    LoopObservation,
    LoopRound,
    LoopWorkerRequest,
)
from backend.app.desktop.agent_loop.observation import observation_hash
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.run_activity_writer import (
    LoopRunActivityWriteError,
    LoopRunActivityWriter,
)
from backend.app.desktop.agent_loop.schemas import AdjustLoopBudgetsRequest
from backend.app.desktop.agent_loop.task_progress.consolidation import (
    TaskProgressConsolidator,
)
from backend.app.desktop.agent_loop.task_progress.contracts import (
    ProgressCandidate,
    RoundDecisionInputs,
    SourceAssessment,
    TaskDeltaManifest,
    TaskItem,
    TaskProgressDocument,
    TaskSource,
    canonical_hash,
)
from backend.app.desktop.agent_loop.task_progress.models import (
    LoopProgressHead,
    LoopProgressReceipt,
    LoopProgressWork,
    LoopTaskProgress,
)
from backend.app.desktop.agent_loop.task_progress.query import TaskProgressQuery
from backend.app.desktop.agent_loop.task_progress.repository import (
    ProgressNotReady,
    ProgressPublicationRejected,
    TaskProgressRepository,
)
from backend.app.desktop.agent_loop.task_progress.runtime import TaskProgressRuntime
from backend.app.desktop.agent_loop.workers import LoopWorkerRuntime
from backend.app.desktop.domain_evidence.repository import DomainResultRepository
from backend.app.desktop.domain_evidence.tests import TestResultParser
from backend.app.desktop.models import DesktopRun, DesktopThread


def _source(*, kind="test", source_id="test-1", payload=None):
    payload = payload or {
        "status": "failed",
        "metrics": {"passed": 41, "failed": 2, "count_status": "exact"},
    }
    version = canonical_hash(payload)
    return TaskSource(
        source_key=canonical_hash([kind, source_id, version]),
        kind=kind,
        source_id=source_id,
        version=version,
        payload=payload,
    )


def _inputs(previous, sources=()):
    manifest = TaskDeltaManifest(boundary="o", sources=sources)
    topology_hash = canonical_hash([])
    return RoundDecisionInputs(
        round_id="r",
        observation_id="o",
        observation_hash="a" * 64,
        previous_progress_id="p",
        previous_progress_hash=canonical_hash(previous),
        previous_progress=previous,
        task_delta=manifest,
        manifest_hash=canonical_hash(manifest),
        lineage={"roots": {}, "nodes": [], "edges": [], "topology_hash": topology_hash},
        topology_hash=topology_hash,
    )


def test_partial_delta_preserves_old_achievements_and_obligations():
    previous = TaskProgressDocument(
        mission_revision=1,
        items=(
            TaskItem(
                item_id="backend",
                description="数据库实现完成",
                state="completed",
                support="supported",
                evidence_keys=("old-source",),
            ),
            TaskItem(item_id="tests", description="测试", state="in_progress"),
            TaskItem(item_id="ui", description="界面", state="not_started"),
        ),
    )
    source = _source()
    changed = TaskItem(
        item_id="tests",
        description="测试",
        state="blocked",
        support="supported",
        evidence_keys=(source.source_key,),
        corrects=("tests",),
        blockers=("两个失败项", "认证问题"),
    )
    document, contribution = TaskProgressConsolidator().apply(
        _inputs(previous, (source,)),
        ProgressCandidate(changes=(changed,)),
        {"revision": 1},
    )
    items = {item.item_id: item for item in document.items}
    assert items["backend"] == previous.items[0]
    assert items["ui"] == previous.items[2]
    assert items["tests"].blockers == ("两个失败项", "认证问题")
    assert contribution.direct_source_keys == (source.source_key,)


def test_asserted_run_success_cannot_prove_completion():
    previous = TaskProgressDocument(mission_revision=1)
    source = _source(
        kind="run_outcome",
        payload={"status": "success", "statement": "数据库完成", "support": "asserted"},
    )
    candidate = ProgressCandidate(
        changes=(
            TaskItem(
                item_id="backend",
                description="完成",
                state="completed",
                support="supported",
                evidence_keys=(source.source_key,),
            ),
        )
    )
    with pytest.raises(ValueError, match="自述"):
        TaskProgressConsolidator().apply(_inputs(previous, (source,)), candidate, {})


def test_multiple_run_contributions_preserve_unresolved_blockers_and_conflicts():
    previous = TaskProgressDocument(
        mission_revision=1,
        items=(
            TaskItem(
                item_id="tests",
                description="测试",
                state="blocked",
                blockers=("两个失败项", "认证问题"),
            ),
            TaskItem(
                item_id="deploy",
                description="部署",
                state="blocked",
                blockers=("等待用户提供环境",),
            ),
        ),
    )
    database = _source(
        kind="artifact", source_id="database", payload={"path": "database.sql"}
    ).model_copy(update={"run_id": "run-a", "execution_round_id": "prior-round"})
    failing = _source(source_id="failing").model_copy(
        update={"run_id": "run-b", "execution_round_id": "prior-round"}
    )
    repaired = _source(
        source_id="auth", payload={"status": "verified", "metrics": {"passed": 1}}
    ).model_copy(update={"run_id": "run-c", "execution_round_id": "r"})
    candidate = ProgressCandidate(
        changes=(
            TaskItem(
                item_id="backend",
                description="数据库实现完成",
                state="completed",
                support="supported",
                evidence_keys=(database.source_key,),
            ),
            TaskItem(
                item_id="auth",
                description="认证回归通过",
                state="completed",
                support="supported",
                evidence_keys=(repaired.source_key,),
            ),
            TaskItem(
                item_id="tests",
                description="测试来源存在冲突，两个失败项待复验",
                state="conflicted",
                support="supported",
                evidence_keys=(failing.source_key, repaired.source_key),
                corrects=("tests",),
                blockers=("两个失败项",),
            ),
        )
    )
    document, contribution = TaskProgressConsolidator().apply(
        _inputs(previous, (database, failing, repaired)), candidate, {}
    )
    items = {item.item_id: item for item in document.items}
    assert items["backend"].state == items["auth"].state == "completed"
    assert items["tests"].state == "conflicted" and items["tests"].blockers == (
        "两个失败项",
    )
    assert items["deploy"] == previous.items[1]
    assert {run.run_id for run in contribution.run_contributions} == {
        "run-a",
        "run-b",
        "run-c",
    }
    assert {run.execution_round_id for run in contribution.run_contributions} == {
        "prior-round",
        "r",
    }
    assert all(run.observed_round_id == "r" for run in contribution.run_contributions)


def test_source_contract_rejects_tool_trace_and_wrong_version():
    with pytest.raises(ValidationError):
        _source(kind="tool")
    with pytest.raises(ValidationError, match="版本"):
        TaskSource(
            source_key=canonical_hash(["test", "x", "bad"]),
            kind="test",
            source_id="x",
            version="bad",
            payload={},
        )


def test_explicit_supersession_preserves_previous_item_and_requires_real_identity():
    old = TaskItem(
        item_id="old-check",
        description="旧测试入口",
        state="blocked",
        blockers=("入口无法执行",),
    )
    previous = TaskProgressDocument(mission_revision=1, items=(old,))
    source = _source(
        kind="workspace", payload={"revision": 2, "adoption_state": "adopted"}
    )
    replacement = TaskItem(
        item_id="new-check",
        description="新测试入口待验证",
        state="in_progress",
        support="supported",
        evidence_keys=(source.source_key,),
        supersedes=("old-check",),
    )
    document, contribution = TaskProgressConsolidator().apply(
        _inputs(previous, (source,)), ProgressCandidate(changes=(replacement,)), {}
    )
    assert (
        document.items[0].state == "not_applicable"
        and document.items[0].blockers == old.blockers
    )
    assert contribution.changes[0].supersedes == ("old-check",)
    with pytest.raises(ValueError, match="前序其他任务"):
        TaskProgressConsolidator().apply(
            _inputs(previous, (source,)),
            ProgressCandidate(
                changes=(replacement.model_copy(update={"supersedes": ("missing",)}),)
            ),
            {},
        )


def test_parser_uses_final_summary_and_does_not_trust_ordinary_text():
    result = TestResultParser.parse(
        "pytest", "41 passed, 2 failed\n41 passed, 2 failed"
    )
    assert result["metrics"]["passed"] == 41
    assert TestResultParser.parse("shell", "I ran pytest and 41 passed") is None
    assert (
        TestResultParser.parse(
            "shell", "output unavailable", command="python -m pytest"
        )["metrics"]["count_status"]
        == "unknown"
    )
    assert TestResultParser.parse("pytest", "2 passed, 1 error")["status"] == "failed"
    assert TestResultParser.parse("pytest", "3 skipped")["status"] == "unknown"
    assert (
        TestResultParser.parse("pytest", "2 passed", execution_status="error")["status"]
        == "failed"
    )


class _Checkpointer:
    async def aget_tuple(self, config):
        return SimpleNamespace(
            config=config, checkpoint={"channel_values": {"messages": []}}, metadata={}
        )


@pytest.mark.usefixtures("isolated_postgres_database")
def test_round_freeze_late_sources_readiness_and_atomic_publication(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = None
        try:
            fixture = await _seed_loop(
                sessions, tmp_path, label="memory", started_at=datetime.now(UTC)
            )
            loop_id = fixture["loop_id"]
            results = DomainResultRepository()
            async with sessions.begin() as session:
                context = await session.get(DesktopThread, fixture["context_id"])
                for index in range(40):
                    run_id = uuid.uuid4().hex
                    session.add(
                        DesktopRun(
                            run_id=run_id,
                            task_id=context.task_id,
                            agent_id=f"main:{context.task_id}",
                            kind="main",
                            status="success",
                            origin="delegated_patrol",
                            execution_thread_id=context.thread_id,
                            context_revision_id=context.current_revision_id,
                            loop_id=loop_id,
                            round_id=fixture["round_id"],
                            settled_at=datetime.now(UTC),
                        )
                    )
                    await session.flush()
                    await results.record(
                        session,
                        kind="test",
                        source_id=f"test-{loop_id}-{index}",
                        loop_id=loop_id,
                        context_id=fixture["context_id"],
                        run_id=run_id,
                        payload={
                            "status": "verified",
                            "metrics": {"passed": index, "count_status": "exact"},
                        },
                    )
            observations = LoopObservationService(sessions, _Checkpointer())
            envelope, repeated_capture = await asyncio.gather(
                observations.capture(loop_id, fixture["round_id"]),
                observations.capture(loop_id, fixture["round_id"]),
            )
            assert repeated_capture.model_dump(mode="json") == envelope.model_dump(
                mode="json"
            )
            assert observation_hash(repeated_capture) == observation_hash(envelope)
            frozen_hash = observation_hash(envelope)
            assert (
                len(
                    [
                        item
                        for item in envelope.task_delta["sources"]
                        if item["kind"] == "test"
                    ]
                )
                == 40
            )
            assert (
                len(
                    [
                        item
                        for item in envelope.task_delta["sources"]
                        if item["kind"] == "run_outcome"
                    ]
                )
                == 41
            )
            supplemented = PatrolDecisionContext(
                envelope, expansion_assessment={"level": "none"}
            ).model_observation()
            assert supplemented.expansion_assessment == {"level": "none"}
            async with sessions.begin() as session:
                late = await results.record(
                    session,
                    kind="test",
                    source_id=f"late-{loop_id}",
                    loop_id=loop_id,
                    context_id=fixture["context_id"],
                    payload={
                        "status": "failed",
                        "metrics": {"failed": 2, "count_status": "exact"},
                    },
                )
                correction = await results.record(
                    session,
                    kind="test",
                    source_id=f"test-{loop_id}-0",
                    loop_id=loop_id,
                    context_id=fixture["context_id"],
                    payload={
                        "status": "failed",
                        "metrics": {"failed": 1, "count_status": "exact"},
                    },
                )
                round_id = uuid.uuid4().hex
                number = (
                    int(
                        await session.scalar(
                            select(func.max(LoopRound.number)).where(
                                LoopRound.loop_id == loop_id
                            )
                        )
                    )
                    + 1
                )
                session.add(
                    LoopRound(
                        round_id=round_id,
                        loop_id=loop_id,
                        number=number,
                        authority_revision=1,
                        goal_revision=1,
                        frontier_hash="a" * 64,
                        workspace_revision=1,
                    )
                )
            with pytest.raises(ProgressNotReady):
                await observations.capture(loop_id, round_id)
            repository = TaskProgressRepository()
            async with sessions.begin() as session:
                inputs = await repository.inputs(session, envelope.decision_inputs_ref)
                assert late not in {
                    item.source_key for item in inputs.task_delta.sources
                }
                work = await session.get(LoopProgressWork, envelope.decision_inputs_ref)
                work.state = "claimed"
                work.fence = 1
                work.lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
            candidate = ProgressCandidate(
                source_assessments=tuple(
                    SourceAssessment(
                        source_key=source.source_key,
                        disposition="unknown",
                        explanation="测试结果尚未关联任务义务",
                    )
                    for source in inputs.task_delta.sources
                )
            )
            document, contribution = TaskProgressConsolidator().apply(
                inputs, candidate, envelope.mission
            )
            assert len(contribution.run_contributions) == 41
            assert (
                sum(
                    item.execution_round_id == inputs.round_id
                    for item in contribution.run_contributions
                )
                >= 40
            )
            with pytest.raises(ProgressPublicationRejected):
                async with sessions.begin() as session:
                    await repository.publish(
                        session, inputs.observation_id, 0, document, contribution
                    )
            with pytest.raises(RuntimeError, match="crash"):
                async with sessions.begin() as session:
                    await repository.publish(
                        session, inputs.observation_id, 1, document, contribution
                    )
                    raise RuntimeError("crash before commit")
            async with sessions() as session:
                assert (await repository.current(session, loop_id)).generation == 0
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(LoopProgressReceipt)
                        .where(LoopProgressReceipt.loop_id == loop_id)
                    )
                    == 0
                )
            async with sessions.begin() as session:
                first = await repository.publish(
                    session, inputs.observation_id, 1, document, contribution
                )
                first_id = first.progress_id
            async with sessions.begin() as session:
                repeated = await repository.publish(
                    session, inputs.observation_id, 1, document, contribution
                )
                assert repeated.progress_id == first_id
                base = await session.get(LoopObservation, inputs.observation_id)
                assert base.envelope_hash == frozen_hash
                assert base.envelope["expansion_assessment"] is None
                assert await session.scalar(
                    select(func.count())
                    .select_from(LoopProgressReceipt)
                    .where(LoopProgressReceipt.loop_id == loop_id)
                ) == len(inputs.task_delta.sources)
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(LoopTaskProgress)
                        .where(LoopTaskProgress.loop_id == loop_id)
                    )
                    == 2
                )
            async with sessions.begin() as session:
                await session.execute(
                    delete(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop_id)
                )
            following = await observations.capture(loop_id, round_id)
            assert {item["source_key"] for item in following.task_delta["sources"]} == {
                late,
                correction,
            }
            assert following.previous_task_progress == document.model_dump(mode="json")
            for kind in ("lane_curator", "retrieval_cognitive_planner"):
                request = LoopWorkerRequest(
                    worker_request_id=uuid.uuid4().hex,
                    loop_id=loop_id,
                    round_id=inputs.round_id,
                    kind=kind,
                    scope={
                        "assignments": [{"context_id": fixture["context_id"]}],
                        "derivation_input": {
                            "decision_inputs_ref": inputs.observation_id
                        }
                        if kind == "lane_curator"
                        else {},
                    },
                )
                cognition, _, _ = await LoopWorkerRuntime(sessions, None)._evidence(
                    request
                )
                assert (
                    cognition["previous_task_progress"]
                    == envelope.previous_task_progress
                )
                assert late not in {
                    source["source_key"]
                    for source in cognition["task_delta"]["sources"]
                }
                assert cognition["workspace"] == envelope.workspace
                assert (
                    cognition["committed_lineage"]["topology_hash"]
                    == envelope.committed_lineage["topology_hash"]
                )
                assert (
                    cognition["scope"]["derivation_input"]["decision_inputs_ref"]
                    == inputs.observation_id
                )
            async with sessions() as session:
                diagnostics = await TaskProgressQuery().read(session, loop_id)
                assert diagnostics["current"]["generation"] == 1
                assert diagnostics["history"][0][
                    "contribution"
                ] == contribution.model_dump(mode="json")
                assert diagnostics["history_has_more"] is False
                first_work = next(
                    work
                    for work in diagnostics["work"]
                    if work["observation_id"] == inputs.observation_id
                )
                assert first_work["observation_hash"] == frozen_hash
                assert first_work["manifest_complete"] is True
                assert all(
                    "payload" not in source and "audit" not in source
                    for source in first_work["source_refs"]
                )
            assert (
                await observations.capture(loop_id, fixture["round_id"])
            ).previous_task_progress == envelope.previous_task_progress
        finally:
            if fixture:
                await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()

    asyncio.run(exercise())


def test_sources_cannot_be_silently_absorbed():
    inputs = _inputs(TaskProgressDocument(mission_revision=1), (_source(),))
    with pytest.raises(ValueError, match="不能静默吸收"):
        TaskProgressConsolidator().apply(inputs, ProgressCandidate(), {})


@pytest.mark.usefixtures("isolated_postgres_database")
def test_legacy_baseline_preserves_observation_and_absorbs_history_once(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(
            sessions, tmp_path, label="legacy-memory", started_at=datetime.now(UTC)
        )
        loop_id = fixture["loop_id"]
        legacy_id = uuid.uuid4().hex
        legacy_document = {"legacy": "immutable historical input", "nested": [1, 2, 3]}
        legacy_hash = canonical_hash(legacy_document)
        try:
            async with sessions.begin() as session:
                await session.execute(
                    delete(LoopProgressHead).where(LoopProgressHead.loop_id == loop_id)
                )
                await session.execute(
                    delete(LoopTaskProgress).where(LoopTaskProgress.loop_id == loop_id)
                )
                session.add(
                    LoopObservation(
                        observation_id=legacy_id,
                        loop_id=loop_id,
                        round_id=fixture["round_id"],
                        envelope=legacy_document,
                        envelope_hash=legacy_hash,
                        projection_sequence=0,
                        base_entity_revisions={},
                    )
                )
                await DomainResultRepository().record(
                    session,
                    kind="test",
                    source_id=f"legacy-test-{loop_id}",
                    loop_id=loop_id,
                    context_id=fixture["context_id"],
                    payload={
                        "status": "failed",
                        "metrics": {"failed": 2, "count_status": "exact"},
                    },
                )
                round_id = uuid.uuid4().hex
                number = (
                    int(
                        await session.scalar(
                            select(func.max(LoopRound.number)).where(
                                LoopRound.loop_id == loop_id
                            )
                        )
                    )
                    + 1
                )
                session.add(
                    LoopRound(
                        round_id=round_id,
                        loop_id=loop_id,
                        number=number,
                        authority_revision=1,
                        goal_revision=1,
                        frontier_hash="a" * 64,
                        workspace_revision=1,
                    )
                )
            observation = await LoopObservationService(
                sessions, _Checkpointer()
            ).capture(loop_id, round_id)
            assert observation.previous_task_progress["baseline_kind"] == "migration"
            assert observation.previous_task_progress["history_complete"] is False
            assert observation.task_delta["sources"] == []
            assert any(
                item["state"] == "blocked"
                for item in observation.previous_task_progress["items"]
            )
            async with sessions() as session:
                baseline = await TaskProgressRepository().current(session, loop_id)
                assert baseline.generation == 0
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(LoopProgressReceipt)
                        .where(LoopProgressReceipt.loop_id == loop_id)
                    )
                    >= 2
                )
                historical = await session.get(LoopObservation, legacy_id)
                assert (
                    historical.envelope == legacy_document
                    and historical.envelope_hash == legacy_hash
                )
            assert await TaskProgressRuntime(sessions, None).drain(loop_id=loop_id) == 1
            async with sessions() as session:
                assert (
                    await TaskProgressRepository().current(session, loop_id)
                ).generation == 1
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(LoopTaskProgress)
                        .where(LoopTaskProgress.loop_id == loop_id)
                    )
                    == 2
                )
        finally:
            await _stop(fixture["service"], loop_id)
            await engine.dispose()

    asyncio.run(exercise())


def test_new_mission_rechecks_obligations_and_retains_achievements():
    previous = TaskProgressDocument(
        mission_revision=1,
        items=(
            TaskItem(
                item_id="backend",
                description="数据库完成",
                state="completed",
                support="supported",
                evidence_keys=("old",),
            ),
            TaskItem(
                item_id="check:tests",
                description="测试通过",
                state="completed",
                support="supported",
                evidence_keys=("old",),
            ),
            TaskItem(
                item_id="check:removed", description="旧标准", state="not_started"
            ),
        ),
    )
    document, _ = TaskProgressConsolidator().apply(
        _inputs(previous),
        ProgressCandidate(),
        {
            "revision": 2,
            "outcome": "新目标",
            "completion_checks": [
                {"check_id": "tests", "claim": "测试通过"},
                {"check_id": "ui", "claim": "界面完成"},
            ],
        },
    )
    items = {item.item_id: item for item in document.items}
    assert items["backend"] == previous.items[0]
    assert items["check:tests"].state == "unknown"
    assert items["check:removed"].state == "not_applicable"
    assert items["check:ui"].state == "not_started"


@pytest.mark.usefixtures("isolated_postgres_database")
def test_explicit_retry_uses_new_budget_without_rewriting_frozen_inputs(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(
            sessions, tmp_path, label="budget-retry", started_at=datetime.now(UTC), budgets={"max_model_calls": 2}
        )
        loop_id = fixture["loop_id"]
        try:
            observation = await LoopObservationService(
                sessions, _Checkpointer()
            ).capture(loop_id, fixture["round_id"])
            frozen_hash = observation_hash(observation)
            call_limit = observation.budget["limits"]["max_model_calls"]
            async with sessions.begin() as session:
                ledger = await session.get(LoopBudgetUsage, loop_id)
                ledger.model_calls = call_limit

            class Model:
                context_window_tokens = 100000
                max_output_tokens = 1000
                last_usage_reported = True
                last_usage = ModelUsage(model_calls=1, input_tokens=17, output_tokens=9)
                calls = 0

                async def invoke(self, schema, system, payload):
                    self.calls += 1
                    return schema(
                        source_assessments=tuple(
                            SourceAssessment(
                                source_key=source["source_key"],
                                disposition="unknown",
                                explanation="尚待任务验证",
                            )
                            for source in payload["task_delta"]["sources"]
                        )
                    )

            model = Model()
            runtime = TaskProgressRuntime(
                sessions, None, model_factory=lambda name: model
            )
            assert await runtime.drain(loop_id=loop_id) == 1
            assert model.calls == 0
            async with sessions() as session:
                assert (
                    await session.get(LoopProgressWork, observation.decision_inputs_ref)
                ).state == "blocked"
                inputs = await TaskProgressRepository().inputs(
                    session, observation.decision_inputs_ref
                )
                initial_run_id = await session.scalar(
                    select(DesktopRun.run_id)
                    .where(DesktopRun.loop_id == loop_id)
                    .limit(1)
                )
            await LoopRunActivityWriter(sessions, loop_id).write(
                CanonicalEventDraft(
                    kind="context.test.completed",
                    entity_type="test_result",
                    entity_id=uuid.uuid4().hex,
                    entity_revision=1,
                    payload={
                        "run_id": initial_run_id,
                        "context_id": fixture["context_id"],
                        "status": "failed",
                        "metrics": {"passed": 41, "failed": 2, "count_status": "exact"},
                    },
                    idempotency_key=uuid.uuid4().hex,
                )
            )
            assert (
                await FactProjector(sessions, _Checkpointer()).project_loop(loop_id)
                >= 1
            )
            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(LoopFact)
                        .where(
                            LoopFact.loop_id == loop_id, LoopFact.fact_type == "test"
                        )
                    )
                    == 1
                )
                assert (
                    await session.get(LoopProgressWork, observation.decision_inputs_ref)
                ).state == "blocked"
            adjusted = await LoopAuthorityService(sessions).mutate(
                loop_id,
                AdjustLoopBudgetsRequest(
                    command="adjust_budgets",
                    budgets={"max_model_calls": call_limit + 2},
                ),
            )
            await runtime.retry(observation.decision_inputs_ref, loop_id=loop_id)
            assert await runtime.drain(loop_id=loop_id) == 1
            async with sessions() as session:
                work = await session.get(
                    LoopProgressWork, observation.decision_inputs_ref
                )
                assert work.state == "published" and model.calls == 1
                assert (
                    work.retry_budget_authorization["grant_revision"]
                    == adjusted["authority_revision"]
                )
                assert (
                    work.usage[0]["budget_authorization"]
                    == work.retry_budget_authorization
                )
                assert (
                    await TaskProgressRepository().inputs(
                        session, observation.decision_inputs_ref
                    )
                    == inputs
                )
                assert (
                    await session.get(LoopObservation, observation.decision_inputs_ref)
                ).envelope_hash == frozen_hash
        finally:
            await _stop(fixture["service"], loop_id)
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_domain_sources_survive_display_journal_failure_and_active_run(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(
            sessions, tmp_path, label="independent", started_at=datetime.now(UTC)
        )
        loop_id = fixture["loop_id"]
        try:
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
                        origin="delegated_patrol",
                        execution_thread_id=context.thread_id,
                        context_revision_id=context.current_revision_id,
                        loop_id=loop_id,
                        round_id=fixture["round_id"],
                    )
                )
            writer = LoopRunActivityWriter(sessions, loop_id)

            async def failed_journal(*args, **kwargs):
                raise RuntimeError("display journal unavailable")

            writer._journal.append = failed_journal
            for kind, entity_type, fields in (
                (
                    "context.test.completed",
                    "test_result",
                    {
                        "status": "failed",
                        "metrics": {"passed": 41, "failed": 2, "count_status": "exact"},
                    },
                ),
                (
                    "context.artifact.observed",
                    "artifact",
                    {"status": "observed", "path": "reports/test.xml"},
                ),
            ):
                with pytest.raises(LoopRunActivityWriteError):
                    await writer.write(
                        CanonicalEventDraft(
                            kind=kind,
                            entity_type=entity_type,
                            entity_id=uuid.uuid4().hex,
                            entity_revision=1,
                            payload={
                                "run_id": run_id,
                                "context_id": fixture["context_id"],
                                **fields,
                            },
                            idempotency_key=uuid.uuid4().hex,
                        )
                    )
            observation = await LoopObservationService(
                sessions, _Checkpointer()
            ).capture(loop_id, fixture["round_id"])
            partial = [
                source
                for source in observation.task_delta["sources"]
                if source["run_id"] == run_id
            ]
            assert {source["kind"] for source in partial} == {"test", "artifact"}
            assert not any(source["kind"] == "run_outcome" for source in partial)
            assert (
                next(source for source in partial if source["kind"] == "test")[
                    "payload"
                ]["metrics"]["passed"]
                == 41
            )
        finally:
            await _stop(fixture["service"], loop_id)
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_independent_runtime_fencing_failure_usage_and_terminal_close(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(
            sessions, tmp_path, label="runtime", started_at=datetime.now(UTC)
        )
        loop_id = fixture["loop_id"]
        try:
            observation = await LoopObservationService(
                sessions, _Checkpointer()
            ).capture(loop_id, fixture["round_id"])
            async with sessions() as session:
                ledger = await session.get(LoopBudgetUsage, loop_id)
                initial_usage = (
                    ledger.model_calls,
                    ledger.input_tokens,
                    ledger.output_tokens,
                )
                directives = await session.scalar(
                    select(func.count())
                    .select_from(LoopDirective)
                    .where(LoopDirective.loop_id == loop_id)
                )

            class Model:
                context_window_tokens = 100000
                max_output_tokens = 1000
                last_usage_reported = True
                last_usage = ModelUsage(model_calls=1, input_tokens=17, output_tokens=9)
                calls = 0

                async def invoke(self, schema, system, payload):
                    self.calls += 1
                    await asyncio.sleep(0.02)
                    if self.calls == 1:
                        raise ValueError("provider failure")
                    return schema(
                        source_assessments=tuple(
                            SourceAssessment(
                                source_key=source["source_key"],
                                disposition="unknown",
                                explanation="冻结结果尚待独立验证",
                            )
                            for source in payload["task_delta"]["sources"]
                        )
                    )

            model = Model()
            runtime = TaskProgressRuntime(
                sessions, None, model_factory=lambda name: model
            )
            await _stop(fixture["service"], loop_id)
            assert sorted(
                await asyncio.gather(
                    runtime.drain(loop_id=loop_id), runtime.drain(loop_id=loop_id)
                )
            ) == [0, 1]
            async with sessions() as session:
                assert (
                    await TaskProgressRepository().current(session, loop_id)
                ).generation == 0
                work = await session.get(
                    LoopProgressWork, observation.decision_inputs_ref
                )
                assert work.state == "pending" and work.attempts == 1
                assert work.usage[0]["accounted"] is True
            recovered_runtime = TaskProgressRuntime(
                sessions, None, model_factory=lambda name: model
            )
            assert await recovered_runtime.drain(loop_id=loop_id) == 1
            assert await runtime.drain(loop_id=loop_id) == 0
            async with sessions() as session:
                assert (
                    await TaskProgressRepository().current(session, loop_id)
                ).generation == 1
                ledger = await session.get(LoopBudgetUsage, loop_id)
                assert (
                    ledger.model_calls - initial_usage[0],
                    ledger.input_tokens - initial_usage[1],
                    ledger.output_tokens - initial_usage[2],
                ) == (2, 34, 18)
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(LoopDirective)
                        .where(LoopDirective.loop_id == loop_id)
                    )
                    == directives
                )
                assert (
                    await session.get(LoopProgressWork, observation.decision_inputs_ref)
                ).state == "published"
        finally:
            await _stop(fixture["service"], loop_id)
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_expired_lease_recovery_rejects_previous_worker_and_reuses_inputs(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(
            sessions, tmp_path, label="lease-recovery", started_at=datetime.now(UTC)
        )
        loop_id = fixture["loop_id"]
        try:
            observation = await LoopObservationService(
                sessions, _Checkpointer()
            ).capture(loop_id, fixture["round_id"])
            first = await TaskProgressRuntime(sessions, None)._claim(loop_id=loop_id)
            async with sessions.begin() as session:
                work = await session.get(
                    LoopProgressWork, first[0], with_for_update=True
                )
                work.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                inputs = await TaskProgressRepository().inputs(session, first[0])
            second = await TaskProgressRuntime(sessions, None)._claim(loop_id=loop_id)
            assert second == (first[0], first[1] + 1)
            candidate = ProgressCandidate(
                source_assessments=tuple(
                    SourceAssessment(
                        source_key=source.source_key,
                        disposition="unknown",
                        explanation="工作恢复后验证冻结来源",
                    )
                    for source in inputs.task_delta.sources
                )
            )
            document, contribution = TaskProgressConsolidator().apply(
                inputs, candidate, observation.mission
            )
            with pytest.raises(ProgressPublicationRejected, match="fence/lease"):
                async with sessions.begin() as session:
                    await TaskProgressRepository().publish(
                        session, first[0], first[1], document, contribution
                    )
            async with sessions.begin() as session:
                await TaskProgressRepository().publish(
                    session, second[0], second[1], document, contribution
                )
            async with sessions() as session:
                assert (
                    await TaskProgressRepository().inputs(session, first[0]) == inputs
                )
                assert (
                    await TaskProgressRepository().current(session, loop_id)
                ).generation == 1
                assert await session.scalar(
                    select(func.count())
                    .select_from(LoopProgressReceipt)
                    .where(LoopProgressReceipt.loop_id == loop_id)
                ) == len(inputs.task_delta.sources)
        finally:
            await _stop(fixture["service"], loop_id)
            await engine.dispose()

    asyncio.run(exercise())
