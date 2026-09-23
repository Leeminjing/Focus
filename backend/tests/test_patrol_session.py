r"""本文件验证可恢复 Patrol Session 状态机、结构化历史与安全事件 payload。

输入为真实 Loop round、合法/非法 phase、等待目标和受禁止的隐藏字段；输出为重启后历史一致、重复 phase 合法、
终态封闭、隐私合同拒绝、合同违例逐次留痕与派生评估可回读断言。具体工作流为跨两个 Repository 实例写入并读取同一 session，
并以真实数据库核对 Patrol attempt 与 observation 的持久化事实。
示例：`pytest backend/tests/test_patrol_session.py`。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop import (
    AgentLoopService,
    LoopCreateRequest,
    LoopKernel,
    PatrolDecisionIntent,
)
from backend.app.desktop.agent_loop.coordinator import CoordinatorClaim
from backend.app.desktop.agent_loop.curator_assignments import (
    CuratorAssignmentRepository,
    CuratorScope,
)
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopDirective,
    LoopObservation,
    LoopPatrolAttempt,
    LoopRound,
    LoopWorkerRequest,
)
from backend.app.desktop.agent_loop.observation import observation_hash
from backend.app.desktop.agent_loop.patrol import (
    PatrolContractViolation,
    PortfolioPatrol,
)
from backend.app.desktop.agent_loop.patrol_audit import PatrolAuditRepository
from backend.app.desktop.agent_loop.patrol_runtime import (
    CuratorCoordinationStage,
    PatrolSessionLifecycle,
)
from backend.app.desktop.agent_loop.patrol_session_models import LoopCuratorAssignment
from backend.app.desktop.agent_loop.patrol_session_repository import (
    PatrolSessionRepository,
)
from backend.app.desktop.agent_loop.patrol_session_state import (
    PatrolActivity,
    PatrolEvidenceReference,
    PatrolPhase,
    PatrolSessionStateMachine,
    PatrolTransitionRejected,
    PatrolWaitTarget,
)
from backend.app.desktop.agent_loop.round_orchestration import (
    LoopObservationService,
    LoopRoundOrchestrator,
)
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.agent_loop.workers import LaneAdviceProposal, LoopWorkerRuntime
from backend.app.desktop.context_evolution import (
    ContextRevisionContract,
    ContextRevisionOriginKind,
    ContextRevisionPayloadMode,
    ContextRevisionProjectionStatus,
    ContextRevisionRef,
    ContextRevisionRepository,
)
from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_state_machine_repeated_and_terminal_rules() -> None:
    machine = PatrolSessionStateMachine()
    assert machine.validate("collecting_curators", "collecting_curators") == PatrolPhase.COLLECTING_CURATORS
    assert machine.validate("authorizing", "awaiting_evidence") == PatrolPhase.AWAITING_EVIDENCE
    with pytest.raises(PatrolTransitionRejected):
        machine.validate("created", "authorizing")
    with pytest.raises(PatrolTransitionRejected):
        machine.validate("completed", "observing")
    with pytest.raises(ValidationError):
        PatrolActivity.model_validate({"summary": "safe", "raw_prompt": "private"})


def test_session_history_survives_repository_restart(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot = await _create_loop(sessions, tmp_path)
        try:
            repository = PatrolSessionRepository()
            async with sessions.begin() as session:
                patrol = await repository.begin(session, snapshot["loop_id"], snapshot["current_round_id"], 7)
                session_id = patrol.session_id
                await repository.transition(session, session_id, PatrolPhase.FREEZING_OBSERVATION, PatrolActivity(summary="正在冻结 Portfolio observation"))
                await repository.transition(session, session_id, PatrolPhase.OBSERVING, PatrolActivity(summary="发现 3 个活动 Context"), observation_id="observation-1", observation_sequence=12)
                await repository.transition(session, session_id, PatrolPhase.DISPATCHING_CURATORS, PatrolActivity(summary="向 3 个 Curator 分派证据检查"))
                await repository.transition(session, session_id, PatrolPhase.COLLECTING_CURATORS, PatrolActivity(summary="收到 Testing Curator 结果"))
                await repository.transition(session, session_id, PatrolPhase.COLLECTING_CURATORS, PatrolActivity(summary="仍在等待 Architecture Curator"))
                await repository.transition(session, session_id, PatrolPhase.PROPOSING, PatrolActivity(summary="正在形成下一步提议"))
                await repository.transition(session, session_id, PatrolPhase.AUTHORIZING, PatrolActivity(summary="正在请求 Kernel 授权"))
                await repository.transition(session, session_id, PatrolPhase.AWAITING_EVIDENCE, PatrolActivity(summary="正在等待 Context A workspace 结果", wait_reason="workspace_evidence", wait_targets=(PatrolWaitTarget(entity_type="context", entity_id="context-a", evidence_category="workspace"),)))
                await repository.transition(session, session_id, PatrolPhase.COMPLETED, PatrolActivity(summary="本轮已完成"), terminal_outcome={"status": "completed"})
            restarted = PatrolSessionRepository()
            async with sessions() as session:
                history = await restarted.history(session, snapshot["loop_id"], snapshot["current_round_id"])
                events = tuple((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.loop_id == snapshot["loop_id"], LoopJournalEvent.entity_type == "patrol_session").order_by(LoopJournalEvent.sequence))).all())
            assert history[0]["to_phase"] == "created"
            assert history[-1]["to_phase"] == "completed"
            assert history[-2]["wait_reason"] == "workspace_evidence"
            assert events[-1].payload["status"] == "completed"
            assert all("raw_prompt" not in event.payload and "chain_of_thought" not in event.payload for event in events)
        finally:
            loop = await service.get(snapshot["loop_id"])
            if loop["status"] in {"running", "paused", "waiting_user"}:
                await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


def test_curator_assignments_publish_partial_progress_without_authority(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot = await _create_loop(sessions, tmp_path)
        lifecycle = PatrolSessionLifecycle(sessions)
        coordination = CuratorCoordinationStage(sessions)
        assignments = CuratorAssignmentRepository()
        runtime = LoopWorkerRuntime(sessions, object(), concurrency=2)
        orchestrator = LoopRoundOrchestrator(sessions, object(), None, None)
        try:
            claim = CoordinatorClaim("lease-1", snapshot["loop_id"], snapshot["current_round_id"], "1")
            patrol = await lifecycle.begin(claim)
            observation = LoopObservationEnvelope(
                loop_id=snapshot["loop_id"],
                loop_revision=snapshot["revision"],
                round_id=snapshot["current_round_id"],
                goal_revision=1,
                authority_revision=1,
                observed_frontier_hash="a" * 64,
                mission={"outcome": "finish"},
                grant={},
                portfolio_frontier=tuple(
                    {
                        "lane_id": f"lane-{index}",
                        "context_id": f"context-{index}",
                        "revision_id": f"revision-{index}",
                        "role": role,
                    }
                    for index, role in enumerate(("implementation", "testing", "architecture"), 1)
                ),
                workspace={"revision": 1},
                budget={},
            )

            class FrozenObservations:
                async def capture(self, loop_id: str, round_id: str):
                    return observation

            orchestrator._observations = FrozenObservations()
            assert await orchestrator._prepare_observation(claim, patrol.session_id, "observed") is None
            claimed = await runtime._claim_many(2)
            assert len(claimed) == 2
            await runtime._start_curator_analysis(claimed[0].worker_request_id)
            async with sessions.begin() as session:
                assignment = await assignments.by_worker(session, claimed[0].worker_request_id, lock=True)
                await assignments.transition(
                    session,
                    assignment.assignment_id,
                    "proposed",
                    "Testing Curator 已提交证据建议",
                    result_summary="tests reveal one failure",
                )
                worker = await session.get(LoopWorkerRequest, claimed[0].worker_request_id, with_for_update=True)
                worker.status = "success"
                worker.result = {"rationale": "tests reveal one failure", "work_specs": []}
                worker.completed_at = datetime.now(UTC)
            async with sessions() as session:
                states = set((await session.scalars(select(LoopCuratorAssignment.state).where(LoopCuratorAssignment.session_id == patrol.session_id))).all())
                proposed_events = tuple((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.loop_id == snapshot["loop_id"], LoopJournalEvent.kind == "curator.assignment.proposed"))).all())
                directive_count = len(tuple((await session.scalars(select(LoopDirective).where(LoopDirective.loop_id == snapshot["loop_id"]))).all()))
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
            assert states == {"queued", "reading", "proposed"}
            assert len(proposed_events) == 1
            assert directive_count == 0
            assert round_row.status == "waiting_workers"
            results = await coordination.results(patrol.session_id)
            assert {item["status"] for item in results} == {"queued", "reading", "proposed"}
            assert await coordination.consume(patrol.session_id) == 1
            async with sessions.begin() as session:
                remaining = tuple(
                    (
                        await session.scalars(
                            select(LoopCuratorAssignment)
                            .where(
                                LoopCuratorAssignment.session_id == patrol.session_id,
                                LoopCuratorAssignment.state.in_(("queued", "reading", "analyzing")),
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                for assignment in remaining:
                    if assignment.state == "queued":
                        assignment = await assignments.transition(session, assignment.assignment_id, "reading", "Curator 正在读取证据")
                    if assignment.state == "reading":
                        assignment = await assignments.transition(session, assignment.assignment_id, "analyzing", "Curator 正在分析证据")
                    await assignments.transition(session, assignment.assignment_id, "proposed", "Curator proposal 已提交", result_summary="safe proposal")
                    worker = await session.get(LoopWorkerRequest, assignment.worker_request_id, with_for_update=True)
                    worker.status = "success"
                    worker.result = {"rationale": "safe proposal", "work_specs": []}
                    worker.completed_at = datetime.now(UTC)
                round_row = await session.get(LoopRound, snapshot["current_round_id"], with_for_update=True)
                round_row.status = "curated"
            curated = await orchestrator._prepare_observation(claim, patrol.session_id, "curated")
            assert curated is not None
            assert len(curated.worker_results) == 3
            current = await lifecycle.current(snapshot["current_round_id"])
            assert current.phase == "proposing"
            await lifecycle.transition(patrol.session_id, PatrolPhase.AUTHORIZING, PatrolActivity(summary="正在请求 Kernel 授权"))
            await lifecycle.transition(patrol.session_id, PatrolPhase.COMPLETED, PatrolActivity(summary="Patrol round 已完成"), terminal_outcome={"status": "completed"})
            async with sessions() as session:
                audit = await PatrolAuditRepository().round_history(session, snapshot["loop_id"], snapshot["current_round_id"])
                phase_events = tuple((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.loop_id == snapshot["loop_id"], LoopJournalEvent.entity_type == "patrol_session").order_by(LoopJournalEvent.sequence))).all())
            assert audit["session"]["status"] == "completed"
            assert audit["phases"][-1]["to_phase"] == "completed"
            assert {item["state"] for item in audit["curators"]} == {"consumed"}
            collecting = next(event for event in phase_events if event.payload["phase"] == "collecting_curators" and event.payload["wait_reason"] == "curator_results")
            proposing = next(event for event in phase_events if event.payload["phase"] == "proposing")
            assert collecting.payload["wait_reason"] == "curator_results"
            assert len(collecting.payload["wait_targets"]) == 3
            assert proposing.payload["wait_reason"] is None
        finally:
            loop = await service.get(snapshot["loop_id"])
            if loop["status"] in {"running", "paused", "waiting_user"}:
                await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


def test_curator_proposal_rejects_direct_authoritative_effects() -> None:
    for effect in ("send_directive", "publish_portfolio", "change_contract", "verify_fact"):
        with pytest.raises(ValidationError):
            LaneAdviceProposal(rationale="attempt", work_specs=({"execute": effect},))
    with pytest.raises(ValidationError):
        CuratorScope.model_validate({"context_id": "c", "revision_id": "r", "raw_prompt": "secret"})
    with pytest.raises(ValidationError):
        PatrolEvidenceReference.model_validate({"kind": "tool", "entity_id": "t", "raw_output": "secret"})


def test_kernel_rejects_forged_and_newer_observation_base_revisions(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot = await _create_loop(sessions, tmp_path)
        try:
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, snapshot["loop_id"], with_for_update=True)
                round_row = await session.get(LoopRound, snapshot["current_round_id"], with_for_update=True)
                base = {"loop": loop.revision, "mission": loop.goal_revision, "grant": loop.authority_revision, "workspace": round_row.workspace_revision}
                session.add(
                    LoopObservation(
                        observation_id=uuid.uuid4().hex,
                        loop_id=loop.loop_id,
                        round_id=round_row.round_id,
                        envelope={},
                        envelope_hash="b" * 64,
                        projection_sequence=12,
                        base_entity_revisions=base,
                    )
                )
            intent = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key=f"base-revision:{uuid.uuid4().hex}",
                loop_id=snapshot["loop_id"],
                loop_revision=snapshot["revision"],
                round_id=snapshot["current_round_id"],
                holder_id=snapshot["holder_id"],
                grant_id=snapshot["grant"]["grant_id"],
                grant_revision=1,
                goal_revision=1,
                observed_frontier_hash="a" * 64,
                observed_workspace_revision=1,
                observed_projection_sequence=12,
                base_entity_revisions=base,
                rationale="wait for evidence",
                actions=({"action": "wait_for_user", "reason": "evidence required"},),
            )
            async with sessions() as session:
                loop = await session.get(AgentLoop, snapshot["loop_id"])
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                forged = intent.model_copy(update={"base_entity_revisions": {**base, "grant": 99}})
                assert await LoopKernel._observation_stale_reason(session, loop, round_row, forged) == "observation_base_revisions_changed"
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, snapshot["loop_id"], with_for_update=True)
                loop.authority_revision = 2
            async with sessions() as session:
                loop = await session.get(AgentLoop, snapshot["loop_id"])
                round_row = await session.get(LoopRound, snapshot["current_round_id"])
                assert await LoopKernel._observation_stale_reason(session, loop, round_row, intent) == "grant_base_revision_changed"
        finally:
            loop = await service.get(snapshot["loop_id"])
            if loop["status"] in {"running", "paused", "waiting_user"}:
                await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


def test_semantic_contract_violation_is_persisted_with_raw_output(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot = await _create_loop(sessions, tmp_path)
        try:
            observation = LoopObservationEnvelope(
                loop_id=snapshot["loop_id"],
                loop_revision=snapshot["revision"],
                round_id=snapshot["current_round_id"],
                goal_revision=1,
                authority_revision=1,
                observed_frontier_hash="a" * 64,
                mission={"outcome": "finish"},
                grant={},
                portfolio_frontier=(),
                workspace={"revision": 1},
                budget={},
            )

            class ViolatingModel:
                def __init__(self) -> None:
                    self.calls = 0

                async def __call__(self, envelope):
                    self.calls += 1
                    raise PatrolContractViolation("spawn_context 引用了当前 assessment 之外的 opportunity", raw_output=f"raw-model-text-{self.calls}")

            patrol = PortfolioPatrol(sessions, ViolatingModel())
            for _ in range(2):
                with pytest.raises(PatrolContractViolation):
                    await patrol.decide(observation, snapshot["holder_id"])

            async with sessions() as session:
                attempts = (
                    await session.scalars(
                        select(LoopPatrolAttempt).where(LoopPatrolAttempt.loop_id == snapshot["loop_id"]).order_by(LoopPatrolAttempt.attempt)
                    )
                ).all()
            assert [row.attempt for row in attempts] == [1, 2]
            assert [row.status for row in attempts] == ["error", "error"]
            assert [row.raw_output for row in attempts] == [{"raw_text": "raw-model-text-1"}, {"raw_text": "raw-model-text-2"}]
            assert all("assessment 之外" in (row.error or "") for row in attempts)
        finally:
            loop = await service.get(snapshot["loop_id"])
            if loop["status"] in {"running", "paused", "waiting_user"}:
                await service.control(snapshot["loop_id"], "stop")
            await engine.dispose()

    asyncio.run(run())


def test_expansion_assessment_is_persisted_and_readable(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service, snapshot = await _create_loop(sessions, tmp_path)
        loop_id, round_id = snapshot["loop_id"], snapshot["current_round_id"]
        try:
            envelope = _observation_envelope(loop_id, round_id)
            async with sessions.begin() as session:
                session.add(
                    LoopObservation(
                        observation_id=uuid.uuid4().hex,
                        loop_id=loop_id,
                        round_id=round_id,
                        envelope=envelope.model_dump(mode="json"),
                        envelope_hash="b" * 64,
                        projection_sequence=0,
                        base_entity_revisions={},
                    )
                )

            observations = LoopObservationService(sessions, None)
            async with sessions() as session:
                row = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == round_id))
                assert row.envelope["expansion_assessment"] is None

            empty = {"level": "not_applicable", "opportunities": [], "blockers": []}
            await observations.attach_expansion_assessment(loop_id, round_id, empty)
            async with sessions() as session:
                row = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == round_id))
                assert row.envelope["expansion_assessment"] == empty

            required = {"level": "required", "opportunities": [{"opportunity_id": "a" * 64}], "blockers": []}
            updated = await observations.attach_expansion_assessment(loop_id, round_id, required)

            assert updated.expansion_assessment == required
            async with sessions() as session:
                row = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == round_id))
                assert row.envelope["expansion_assessment"] == required
                assert row.envelope_hash == observation_hash(updated)
            assert (await observations.capture(loop_id, round_id)).expansion_assessment == required
        finally:
            loop = await service.get(loop_id)
            if loop["status"] in {"running", "paused", "waiting_user"}:
                await service.control(loop_id, "stop")
            await engine.dispose()

    asyncio.run(run())


def _observation_envelope(loop_id: str, round_id: str) -> LoopObservationEnvelope:
    return LoopObservationEnvelope(
        loop_id=loop_id,
        loop_revision=1,
        round_id=round_id,
        goal_revision=1,
        authority_revision=1,
        observed_frontier_hash="a" * 64,
        mission={"outcome": "finish"},
        grant={},
        portfolio_frontier=(),
        workspace={"revision": 1},
        budget={},
    )


async def _create_loop(sessions, tmp_path):
    suffix = uuid.uuid4().hex[:8]
    workspace_id = f"ws-session-{suffix}"
    context_id = f"context-session-{suffix}"
    revision_id = uuid.uuid4().hex
    loop_id = uuid.uuid4().hex
    workspace_path = tmp_path / workspace_id
    workspace_path.mkdir()
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(workspace_path), display_name="session"))
        await session.flush()
        session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=f"thread-{suffix}", title="session"))
        await session.flush()
        ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=f"thread-{suffix}", checkpoint_ns="", checkpoint_id="checkpoint-1", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
        repository = ContextRevisionRepository()
        await repository.insert(session, ContextRevisionContract(ref=ref, content_hash="e" * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.ROOT, created_at=datetime.now(UTC)))
        await repository.switch_current(session, ref, None)
        session.add(DesktopRun(run_id=f"initial-{suffix}", task_id=context_id, agent_id=f"main:{context_id}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{suffix}", context_revision_id=revision_id, settled_at=datetime.now(UTC)))
    service = AgentLoopService(sessions)
    snapshot = await service.start(LoopCreateRequest(loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id, initial_run_id=f"initial-{suffix}", holder_id="patrol-session", goal="Observe the round", task_contract="Expose safe activity only", acceptance_criteria=({"criterion_id": "done", "text": "round audited"},), capabilities=("continue_context", "request_completion"), context_scope=(context_id,), permission_scope=("read",)))
    return service, snapshot
