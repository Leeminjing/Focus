"""本文件对外提供 Round 冻结、真实 Portfolio 发布和后台记忆失败的边界验收测试。

输入为隔离 PostgreSQL、生产发布服务及指定事务位置的并发/故障；输出为一致快照、回滚和有限恢复断言。
具体工作流为准备候选，交错提交与冻结，检查三项持久输入，再验证旧轮读取、新轮派生和预算边界。
示例：python -m pytest backend/tests/test_round_memory_boundaries.py -q；不调用外部模型。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from config_helpers import app_config_for
from focus.runtime.runs.usage import ModelUsage
from round_memory_drill_support import ROOT, DrillProcess
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_agent_loop_round_liveness import _seed_loop, _stop
from test_atomic_portfolio_publication import _CheckpointWriter, _compiled, _contract
from test_round_task_progress import _Checkpointer

from backend.app.desktop.agent_loop.authority_control import LoopAuthorityService
from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim
from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopContextMembership,
    LoopDelegationGrant,
    LoopDirective,
    LoopObservation,
    LoopRound,
    LoopWorkerRequest,
)
from backend.app.desktop.agent_loop.observation import observation_hash
from backend.app.desktop.agent_loop.observation_capture import (
    LoopObservationService,
    _PreparedViewsChanged,
)
from backend.app.desktop.agent_loop.round_orchestration import LoopRoundOrchestrator
from backend.app.desktop.agent_loop.schemas import RevokeLoopGrantRequest
from backend.app.desktop.agent_loop.task_progress.contracts import SourceAssessment
from backend.app.desktop.agent_loop.task_progress.models import (
    LoopDecisionInputs,
    LoopProgressHead,
    LoopProgressReceipt,
    LoopProgressWork,
    LoopTaskProgress,
)
from backend.app.desktop.agent_loop.task_progress.repository import (
    ProgressNotReady,
    TaskProgressRepository,
)
from backend.app.desktop.agent_loop.task_progress.runtime import TaskProgressRuntime
from backend.app.desktop.agent_loop.workers import LoopWorkerRuntime
from backend.app.desktop.context_curation import (
    AtomicPortfolioPublisher,
    CurationLane,
    CurationProgramRepository,
    PortfolioCandidatePreparer,
    PortfolioFreezer,
    PortfolioFreezeRequest,
    PortfolioLaneAction,
    PortfolioLaneIntent,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionPublisher,
    ContextRevisionRepository,
)
from backend.app.desktop.models import DesktopThread

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@asynccontextmanager
async def _world(tmp_path):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixture = None
    try:
        fixture = await _seed_loop(
            sessions, tmp_path, label="boundary", started_at=datetime.now(UTC)
        )
        yield sessions, fixture
    finally:
        if fixture:
            await _stop(fixture["service"], fixture["loop_id"])
        await engine.dispose()


class _Model:
    context_window_tokens = 100000
    max_output_tokens = 1000
    last_usage_reported = True
    last_usage = ModelUsage(model_calls=1, input_tokens=17, output_tokens=9)

    def __init__(self, *, failing=False, window=100000):
        self.calls = 0
        self.failing = failing
        self.context_window_tokens = window

    async def invoke(self, schema, system, payload):
        self.calls += 1
        if self.failing:
            raise ValueError("boundary provider failure")
        return schema(
            source_assessments=tuple(
                SourceAssessment(
                    source_key=source["source_key"],
                    disposition="unknown",
                    explanation="领域结果尚待任务验证",
                )
                for source in payload["task_delta"]["sources"]
            )
        )


async def _prepare_portfolio(sessions, fixture, *, action="update"):
    revisions = ContextRevisionRepository()
    programs = CurationProgramRepository()
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, fixture["loop_id"])
        if action == "create":
            source = await revisions.current(session, fixture["context_id"])
        else:
            context_id = uuid.uuid4().hex
            session.add(
                DesktopThread(
                    task_id=context_id,
                    workspace_id=loop.workspace_id,
                    thread_id=f"thread-{context_id}",
                    title="Backend",
                )
            )
            await session.flush()
            source = _contract(context_id, 1, "Backend evidence")
            await revisions.insert(session, source)
            await revisions.switch_current(session, source.ref, None)
        if action == "update":
            lane = await session.scalar(
                select(CurationLane).where(
                    CurationLane.managed_context_id == fixture["context_id"]
                )
            )
            program_id = lane.program_id
        else:
            program = await programs.create(session, loop.workspace_id)
            program_id = program.program_id
            lane = await programs.add_lane(session, program_id, "E2E")
        request = PortfolioFreezeRequest(
            program_id=program_id,
            source_frontier=(source.ref,),
            lane_intents=(
                PortfolioLaneIntent(
                    lane_id=lane.lane_id,
                    action=PortfolioLaneAction(action),
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
        lane.lane_id, lane.purpose, source.ref, "Test → E2E"
    ).model_copy(update={"action": action, "semantic_fingerprint": "e" * 64})
    await PortfolioCandidatePreparer(
        sessions, ContextRevisionPublisher(revisions, _CheckpointWriter()), revisions
    ).prepare(frozen.portfolio_revision_id, {frozen.candidate_ids[0]: compiled})
    return frozen, AtomicPortfolioPublisher(sessions, revisions)


async def _counts(sessions, loop_id):
    async with sessions() as session:
        return {
            model.__tablename__: await session.scalar(
                select(func.count()).select_from(model).where(model.loop_id == loop_id)
            )
            for model in (
                LoopObservation,
                LoopDecisionInputs,
                LoopProgressWork,
                LoopTaskProgress,
                LoopProgressHead,
                LoopProgressReceipt,
            )
        }


async def _next_round(sessions, fixture):
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
        number = await session.scalar(
            select(func.max(LoopRound.number)).where(LoopRound.loop_id == loop.loop_id)
        )
        row = LoopRound(
            round_id=uuid.uuid4().hex,
            loop_id=loop.loop_id,
            number=number + 1,
            authority_revision=loop.authority_revision,
            goal_revision=loop.goal_revision,
            frontier_hash="a" * 64,
            workspace_revision=1,
        )
        session.add(row)
        loop.current_round_id = row.round_id
        return row.round_id


@pytest.mark.parametrize("commit_at", ["prepared", "inside_snapshot"])
def test_real_portfolio_publication_races_with_freeze(tmp_path, monkeypatch, commit_at):
    async def exercise():
        async with _world(tmp_path) as (sessions, fixture):
            frozen, publisher = await _prepare_portfolio(sessions, fixture)
            capture = LoopObservationService(sessions, _Checkpointer())
            ready, release = asyncio.Event(), asyncio.Event()
            attempts = 0
            if commit_at == "prepared":
                original = capture._prepare_views

                async def intercept(loop_id):
                    nonlocal attempts
                    attempts += 1
                    views = await original(loop_id)
                    if attempts == 1:
                        ready.set()
                        await release.wait()
                    return views

                monkeypatch.setattr(capture, "_prepare_views", intercept)
            else:
                original = capture._build

                async def intercept(session, loop, round_row, views):
                    nonlocal attempts
                    attempts += 1
                    ready.set()
                    await release.wait()
                    return await original(session, loop, round_row, views)

                monkeypatch.setattr(capture, "_build", intercept)
            async with asyncio.timeout(10):
                pending = asyncio.create_task(
                    capture.capture(fixture["loop_id"], fixture["round_id"])
                )
                try:
                    await ready.wait()
                    published = await publisher.publish(
                        frozen.portfolio_revision_id, frozen.controls
                    )
                finally:
                    release.set()
                observation = await pending
            current = published.context_revisions[0].revision_id
            expected = observation.portfolio_frontier[0]["revision_id"]
            assert expected in {current, fixture["revision_id"]}
            if commit_at == "prepared":
                assert expected == current
            assert observation.portfolio_frontier[0]["revision_id"] == expected
            assert (
                observation.base_entity_revisions[f"context:{fixture['context_id']}"]
                == expected
            )
            assert (
                observation.committed_lineage["roots"][fixture["context_id"]]
                == expected
            )
            assert bool(observation.committed_lineage["edges"]) == (expected == current)
            assert attempts == (2 if expected == current else 1)
            assert observation_hash(
                await capture.capture(fixture["loop_id"], fixture["round_id"])
            ) == observation_hash(observation)
            counts = await _counts(sessions, fixture["loop_id"])
            assert (
                counts["loop_observations"]
                == counts["loop_decision_inputs"]
                == counts["loop_progress_work"]
                == 1
            )
            async with sessions() as session:
                context = await session.get(DesktopThread, fixture["context_id"])
                assert context.current_revision_id == current

    asyncio.run(exercise())


@pytest.mark.parametrize("code", ["prepared", "40001", "40P01", "23514"])
@pytest.mark.parametrize("failures", [1, 3])
def test_freeze_retries_whole_transaction_without_partial_records(
    tmp_path, monkeypatch, code, failures
):
    async def exercise():
        async with _world(tmp_path) as (sessions, fixture):
            capture = LoopObservationService(sessions, _Checkpointer())
            original = capture._progress.freeze
            attempts = 0

            async def conflict(session, *args):
                nonlocal attempts
                attempts += 1
                result = await original(session, *args)
                await session.flush()
                if attempts <= failures:
                    if code == "prepared":
                        raise _PreparedViewsChanged("injected prepared-view change")
                    await session.execute(
                        text(
                            f"DO $$ BEGIN RAISE EXCEPTION 'injected freeze conflict' USING ERRCODE = '{code}'; END $$"
                        )
                    )
                return result

            monkeypatch.setattr(capture._progress, "freeze", conflict)
            success = failures == 1 and code != "23514"
            if success:
                observation = await capture.capture(
                    fixture["loop_id"], fixture["round_id"]
                )
                assert attempts == 2
                assert observation_hash(
                    await capture.capture(fixture["loop_id"], fixture["round_id"])
                ) == observation_hash(observation)
            else:
                error = _PreparedViewsChanged if code == "prepared" else DBAPIError
                with pytest.raises(error):
                    await capture.capture(fixture["loop_id"], fixture["round_id"])
                assert attempts == (1 if code == "23514" else 3)
            counts = await _counts(sessions, fixture["loop_id"])
            assert counts == {
                "loop_observations": int(success),
                "loop_decision_inputs": int(success),
                "loop_progress_work": int(success),
                "loop_task_progress": 1,
                "loop_progress_heads": 1,
                "loop_progress_receipts": 0,
            }

    asyncio.run(exercise())


def test_successful_child_publication_keeps_old_round_and_advances_next(tmp_path):
    async def exercise():
        async with _world(tmp_path) as (sessions, fixture):
            frozen, publisher = await _prepare_portfolio(
                sessions, fixture, action="create"
            )
            capture = LoopObservationService(sessions, _Checkpointer())
            old = await capture.capture(fixture["loop_id"], fixture["round_id"])
            before = old.model_dump(mode="json")
            published = await publisher.publish(
                frozen.portfolio_revision_id, frozen.controls
            )
            child = published.context_revisions[0]
            async with sessions.begin() as session:
                session.add(
                    LoopContextMembership(
                        membership_id=uuid.uuid4().hex,
                        loop_id=fixture["loop_id"],
                        context_id=child.context_id,
                        role="derived",
                    )
                )
            round_id = await _next_round(sessions, fixture)
            with pytest.raises(ProgressNotReady):
                await capture.capture(fixture["loop_id"], round_id)
            assert (
                await TaskProgressRuntime(
                    sessions, None, model_factory=lambda _: _Model()
                ).drain(loop_id=fixture["loop_id"])
                == 1
            )
            for kind in ("lane_curator", "retrieval_cognitive_planner"):
                request = LoopWorkerRequest(
                    worker_request_id=uuid.uuid4().hex,
                    loop_id=fixture["loop_id"],
                    round_id=fixture["round_id"],
                    kind=kind,
                    scope={"assignments": [{"context_id": fixture["context_id"]}]},
                )
                cognition, _, _ = await LoopWorkerRuntime(sessions, None)._evidence(
                    request
                )
                assert cognition["committed_lineage"] == old.committed_lineage
                assert cognition["previous_task_progress"] == old.previous_task_progress
            retried = await LoopObservationService(sessions, _Checkpointer()).capture(
                fixture["loop_id"], fixture["round_id"]
            )
            assert retried.model_dump(mode="json") == before
            following = await capture.capture(fixture["loop_id"], round_id)
            assert (
                following.committed_lineage["roots"][child.context_id]
                == child.revision_id
            )
            assert any(
                edge["source_context_id"] == fixture["context_id"]
                and edge["target_context_id"] == child.context_id
                for edge in following.committed_lineage["edges"]
            )
            assert (
                following.committed_lineage["topology_hash"]
                != old.committed_lineage["topology_hash"]
            )
            assert following.previous_task_progress != old.previous_task_progress
            async with sessions() as session:
                stored = await session.get(LoopObservation, old.decision_inputs_ref)
                assert (
                    stored.envelope == before
                    and stored.envelope_hash == observation_hash(old)
                )

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["window", "provider"])
def test_memory_bounds_leave_frozen_inputs_and_receipts_unchanged(tmp_path, mode):
    async def exercise():
        async with _world(tmp_path) as (sessions, fixture):
            observation = await LoopObservationService(
                sessions, _Checkpointer()
            ).capture(fixture["loop_id"], fixture["round_id"])
            model = _Model(
                failing=mode == "provider", window=1 if mode == "window" else 100000
            )
            runtime = TaskProgressRuntime(sessions, None, model_factory=lambda _: model)
            async with sessions() as session:
                before = await TaskProgressRepository().inputs(
                    session, observation.decision_inputs_ref
                )
                usage_before = (
                    await session.get(LoopBudgetUsage, fixture["loop_id"])
                ).model_calls
            for index in range(1 if mode == "window" else 3):
                assert await runtime.drain(loop_id=fixture["loop_id"]) == 1
                async with sessions() as session:
                    work = await session.get(
                        LoopProgressWork, observation.decision_inputs_ref
                    )
                    assert work.attempts == index + 1
                    assert work.state == (
                        "blocked" if mode == "window" or index == 2 else "pending"
                    )
            assert await runtime.drain(loop_id=fixture["loop_id"]) == 0
            next_id = await _next_round(sessions, fixture)
            with pytest.raises(ProgressNotReady):
                await LoopObservationService(sessions, _Checkpointer()).capture(
                    fixture["loop_id"], next_id
                )
            async with sessions() as session:
                work = await session.get(
                    LoopProgressWork, observation.decision_inputs_ref
                )
                assert (
                    "context_budget" if mode == "window" else "provider failure"
                ) in work.error
                assert (
                    await TaskProgressRepository().inputs(
                        session, observation.decision_inputs_ref
                    )
                    == before
                )
                assert (
                    await TaskProgressRepository().current(session, fixture["loop_id"])
                ).generation == 0
                assert (
                    await session.get(LoopBudgetUsage, fixture["loop_id"])
                ).model_calls - usage_before == (0 if mode == "window" else 3)
                assert model.calls == (0 if mode == "window" else 3)
                assert not list(
                    await session.scalars(
                        select(LoopDirective).where(
                            LoopDirective.loop_id == fixture["loop_id"]
                        )
                    )
                )
            assert (await _counts(sessions, fixture["loop_id"]))[
                "loop_progress_receipts"
            ] == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("command", ["revoke", "stop", "expired", "superseded"])
@pytest.mark.parametrize("entry", ["capture", "orchestrator"])
def test_authority_revoked_after_prepare_does_not_freeze_memory(
    tmp_path, monkeypatch, command, entry
):
    async def exercise():
        async with _world(tmp_path) as (sessions, fixture):
            capture = LoopObservationService(sessions, _Checkpointer())
            original = capture._prepare_views

            async def revoke(loop_id):
                views = await original(loop_id)
                if command == "revoke":
                    await LoopAuthorityService(sessions).mutate(
                        loop_id, RevokeLoopGrantRequest(command="revoke")
                    )
                elif command == "stop":
                    await fixture["service"].control(loop_id, "stop")
                else:
                    async with sessions.begin() as session:
                        if command == "expired":
                            grant = await session.scalar(
                                select(LoopDelegationGrant).where(
                                    LoopDelegationGrant.loop_id == loop_id,
                                    LoopDelegationGrant.status == "active",
                                )
                            )
                            grant.expires_at = datetime(2000, 1, 1, tzinfo=UTC)
                        else:
                            row = await session.get(LoopRound, fixture["round_id"])
                            row.status = "superseded"
                return views

            monkeypatch.setattr(capture, "_prepare_views", revoke)
            if entry == "capture":
                with pytest.raises(
                    RuntimeError, match="observation_authority_superseded"
                ):
                    await capture.capture(fixture["loop_id"], fixture["round_id"])
            else:
                orchestrator = LoopRoundOrchestrator(
                    sessions,
                    app_config_for("patrol-test", None),
                    LoopKernel(sessions),
                    _Checkpointer(),
                )
                monkeypatch.setattr(orchestrator, "_observations", capture)
                claim = CoordinatorClaim(
                    lease_id=uuid.uuid4().hex,
                    loop_id=fixture["loop_id"],
                    round_id=fixture["round_id"],
                    fencing_token=uuid.uuid4().hex,
                )
                assert await orchestrator.process(claim) is None
            counts = await _counts(sessions, fixture["loop_id"])
            assert (
                counts["loop_observations"]
                == counts["loop_decision_inputs"]
                == counts["loop_progress_work"]
                == counts["loop_progress_receipts"]
                == 0
            )
            assert counts["loop_task_progress"] == counts["loop_progress_heads"] == 1

    asyncio.run(exercise())


def test_terminal_loop_can_replay_existing_frozen_inputs(tmp_path):
    async def exercise():
        async with _world(tmp_path) as (sessions, fixture):
            capture = LoopObservationService(sessions, _Checkpointer())
            original = await capture.capture(fixture["loop_id"], fixture["round_id"])
            await fixture["service"].control(fixture["loop_id"], "stop")
            restored = await capture.capture(fixture["loop_id"], fixture["round_id"])
            assert restored.model_dump(mode="json") == original.model_dump(mode="json")
            assert observation_hash(restored) == observation_hash(original)

    asyncio.run(exercise())


def test_process_restart_between_curator_and_patrol_reuses_frozen_world(tmp_path):
    async def exercise():
        processes = []
        try:
            async with _world(tmp_path) as (sessions, fixture):
                frozen, publisher = await _prepare_portfolio(sessions, fixture)
                observation = await LoopObservationService(
                    sessions, _Checkpointer()
                ).capture(fixture["loop_id"], fixture["round_id"])
                curator = DrillProcess(
                    ROOT,
                    tmp_path / "curator",
                    "hold-cognition",
                    fixture["loop_id"],
                    observation.decision_inputs_ref,
                )
                processes.append(curator)
                before = await curator.signal("cognition_ready")
                curator.close()
                assert curator.evidence["killed"] is True
                await publisher.publish(frozen.portfolio_revision_id, frozen.controls)
                assert (
                    await TaskProgressRuntime(
                        sessions, None, model_factory=lambda _: _Model()
                    ).drain(loop_id=fixture["loop_id"])
                    == 1
                )
                patrol = DrillProcess(
                    ROOT,
                    tmp_path / "patrol",
                    "read-cognition",
                    fixture["loop_id"],
                    observation.decision_inputs_ref,
                )
                processes.append(patrol)
                after = await patrol.signal("cognition_ready")
                await patrol.finish()
                assert after["pid"] != before["pid"]
                assert {
                    key: value for key, value in before.items() if key != "pid"
                } == {key: value for key, value in after.items() if key != "pid"}
                assert after["base"] == observation.model_dump(mode="json")
                assert after["base_hash"] == observation_hash(observation)
                assert (
                    after["combined"]["worker_results"]
                    == after["supplement"]["results"]
                )
        finally:
            for process in processes:
                process.close()

    asyncio.run(exercise())
