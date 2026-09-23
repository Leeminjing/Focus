r"""本文件验证 Live Loop Execution Plane 的真实生产组件验收场景。

输入为真实 PostgreSQL Loop、三个 Context、生产 Patrol/Curator/Kernel/Dispatcher、Run activity bridge、FactProjector 与
Portfolio event producer；输出为三路同时运行、授权 Directive 改变目标 Context 当前动作、Fact 生命周期和 Portfolio 发布的
持久事件断言。具体工作流为用确定性端口替代模型与 Agent 执行，但不手写任何最终 UI 事件。示例：
`pytest backend/tests/test_loop_live_acceptance.py`。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from focus.runtime.stream_bridge.memory import MemoryStreamBridge
from focus.runtime.stream_bridge.schemas import StreamEvent
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
)
from backend.app.desktop.agent_loop.dispatch import LoopWaveDispatcher
from backend.app.desktop.agent_loop.fact_models import LoopFact, LoopFactRevision
from backend.app.desktop.agent_loop.fact_projector import FactProjector
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.live_projection_projector import (
    LoopLiveSnapshotProjector,
)
from backend.app.desktop.agent_loop.models import (
    LoopDirective,
    LoopRound,
    LoopWorkerRequest,
)
from backend.app.desktop.agent_loop.patrol_runtime import (
    CuratorCoordinationStage,
    PatrolSessionLifecycle,
)
from backend.app.desktop.agent_loop.patrol_session_state import (
    PatrolActivity,
    PatrolPhase,
)
from backend.app.desktop.agent_loop.portfolio_events import (
    PortfolioPublicationEventRecorder,
)
from backend.app.desktop.agent_loop.run_activity_bridge import LoopRunActivityBridge
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
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


def test_production_components_drive_the_complete_live_multi_context_story(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        suffix = uuid.uuid4().hex[:8]
        loop_id = uuid.uuid4().hex
        workspace_id = f"ws-live-acceptance-{suffix}"
        roles = ("implementation", "testing", "architecture")
        context_ids = tuple(f"context-{role}-{suffix}" for role in roles)
        revision_ids = tuple(uuid.uuid4().hex for _ in roles)
        initial_run_id = f"initial-{suffix}"
        service = AgentLoopService(sessions)
        started = False
        try:
            await _seed_contexts(sessions, tmp_path, workspace_id, context_ids, revision_ids, initial_run_id, suffix)
            snapshot = await service.start(
                LoopCreateRequest(
                    loop_id=loop_id,
                    workspace_id=workspace_id,
                    initial_context_id=context_ids[0],
                    initial_run_id=initial_run_id,
                    holder_id=f"patrol-{suffix}",
                    goal="Expose simultaneous Context work",
                    task_contract="Only committed production transitions may drive the UI",
                    acceptance_criteria=({"criterion_id": "live", "text": "live scenario is traceable"},),
                    capabilities=("continue_context", "request_lane_curator", "request_completion"),
                    context_scope=context_ids,
                    permission_scope=("read", "view_evidence"),
                    equipment={"permissions": ["read"]},
                )
            )
            started = True
            patrol = await _fan_out_curators(sessions, snapshot, context_ids, revision_ids, roles)
            messages = (
                "实现目标变更并持续报告工具活动。",
                "请改为复现 Windows 路径失败并运行精确测试。",
                "审查实现与测试证据的一致性。",
            )
            intent = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key=f"acceptance-decision-{suffix}",
                loop_id=loop_id,
                loop_revision=snapshot["revision"],
                round_id=snapshot["current_round_id"],
                holder_id=snapshot["holder_id"],
                grant_id=snapshot["grant"]["grant_id"],
                grant_revision=snapshot["authority_revision"],
                goal_revision=snapshot["goal_revision"],
                observed_frontier_hash=await _round_frontier(sessions, snapshot["current_round_id"]),
                observed_workspace_revision=1,
                rationale="三个 Context 可以并行收集互补证据。",
                actions=tuple({"action": "continue_context", "context_id": context_id, "context_revision_id": revision_id, "message": message} for context_id, revision_id, message in zip(context_ids, revision_ids, messages, strict=True)),
            )
            committed = await LoopKernel(sessions).commit(intent)
            assert committed.status == "committed"
            assert len(committed.directive_ids) == 3
            launched = await LoopWaveDispatcher(sessions, _RunningRunLauncher(sessions)).dispatch(loop_id, snapshot["current_round_id"], 3)
            assert len(launched) == 3
            async with sessions() as session:
                directives = tuple((await session.scalars(select(LoopDirective).where(LoopDirective.directive_id.in_(committed.directive_ids)).order_by(LoopDirective.created_at))).all())
                runs = tuple((await session.scalars(select(DesktopRun).where(DesktopRun.run_id.in_(launched)))).all())
            testing_directive = next(item for item in directives if item.target_context_id == context_ids[1])
            testing_run = next(item for item in runs if item.task_id == context_ids[1])
            await _publish_testing_tool_activity(sessions, loop_id, testing_directive, testing_run, suffix)
            projector = FactProjector(sessions, object())
            assert await projector.project_loop(loop_id) >= 1
            portfolio_id = f"portfolio-{suffix}"
            async with sessions.begin() as session:
                await PortfolioPublicationEventRecorder().record(session, loop_id=loop_id, portfolio_id=portfolio_id, generation=1, round_id=snapshot["current_round_id"], decision_id=intent.decision_id, directive_ids=committed.directive_ids)
            async with sessions.begin() as session:
                projection = await LoopLiveSnapshotProjector().rebuild(session, loop_id)
            async with sessions() as session:
                persisted = tuple((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop_id).order_by(LoopJournalEvent.sequence))).all())
                tool_fact = await session.scalar(select(LoopFact).where(LoopFact.loop_id == loop_id, LoopFact.fact_type == "tool", LoopFact.source_run_id == testing_run.run_id))
                fact_states = tuple((await session.scalars(select(LoopFactRevision.state).where(LoopFactRevision.fact_id == tool_fact.fact_id).order_by(LoopFactRevision.revision))).all())
            assert patrol.phase == PatrolPhase.AUTHORIZING
            assert len(projection.curators) == 3
            assert {projection.runs[run_id].state["context_id"] for run_id in launched} == set(context_ids)
            assert all(projection.runs[run_id].state["status"] == "running" for run_id in launched)
            assert projection.directives[testing_directive.directive_id].state["state"] == "run_started"
            assert projection.runs[testing_run.run_id].state["current_action"] == messages[1]
            assert fact_states[0] == "observed" and "verifying" in fact_states and fact_states[-1] == "verified"
            assert projection.facts[tool_fact.fact_id].state["status"] == "verified"
            assert projection.portfolio.entity_id == portfolio_id
            assert projection.portfolio.state["status"] == "published"
            assert [event.sequence for event in persisted] == list(range(1, len(persisted) + 1))
            persisted_ids = {event.event_id for event in persisted}
            assert all(item.event_id in persisted_ids for item in projection.activity_timeline)
            assert "context.run.trajectory.changed" not in {event.kind for event in persisted}
        finally:
            if started:
                current = await service.get(loop_id)
                if current["status"] in {"running", "paused", "waiting_user"}:
                    await service.control(loop_id, "stop")
            await engine.dispose()

    asyncio.run(run())


async def _seed_contexts(sessions, tmp_path, workspace_id, context_ids, revision_ids, initial_run_id, suffix) -> None:
    async with sessions.begin() as session:
        session.add(DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="live acceptance"))
        await session.flush()
        repository = ContextRevisionRepository()
        for index, (context_id, revision_id) in enumerate(zip(context_ids, revision_ids, strict=True), 1):
            thread_id = f"thread-{context_id}"
            session.add(DesktopThread(task_id=context_id, workspace_id=workspace_id, thread_id=thread_id, title=context_id))
            await session.flush()
            ref = ContextRevisionRef(context_id=context_id, revision_id=revision_id, generation=1, execution_thread_id=thread_id, checkpoint_ns="", checkpoint_id=f"checkpoint-{index}-{suffix}", payload_mode=ContextRevisionPayloadMode.CHECKPOINT)
            await repository.insert(session, ContextRevisionContract(ref=ref, content_hash=str(index) * 64, projection_status=ContextRevisionProjectionStatus.VALID, origin_kind=ContextRevisionOriginKind.ROOT, created_at=datetime.now(UTC)))
            await repository.switch_current(session, ref, None)
        session.add(DesktopRun(run_id=initial_run_id, task_id=context_ids[0], agent_id=f"main:{context_ids[0]}", kind="main", status="success", origin="direct_user", execution_thread_id=f"thread-{context_ids[0]}", context_revision_id=revision_ids[0], settled_at=datetime.now(UTC)))


async def _fan_out_curators(sessions, snapshot, context_ids, revision_ids, roles):
    claim = CoordinatorClaim("acceptance-lease", snapshot["loop_id"], snapshot["current_round_id"], "1")
    lifecycle = PatrolSessionLifecycle(sessions)
    patrol = await lifecycle.begin(claim)
    patrol = await lifecycle.transition(patrol.session_id, PatrolPhase.OBSERVING, PatrolActivity(summary="发现 3 个活动 Context"))
    patrol = await lifecycle.transition(patrol.session_id, PatrolPhase.DISPATCHING_CURATORS, PatrolActivity(summary="向 3 个 Curator 分派证据检查"))
    observation = LoopObservationEnvelope(loop_id=snapshot["loop_id"], loop_revision=snapshot["revision"], round_id=snapshot["current_round_id"], goal_revision=snapshot["goal_revision"], authority_revision=snapshot["authority_revision"], observed_frontier_hash=await _round_frontier(sessions, snapshot["current_round_id"]), mission={"outcome": "Expose simultaneous Context work"}, grant={}, portfolio_frontier=tuple({"lane_id": role, "context_id": context_id, "revision_id": revision_id, "role": role} for role, context_id, revision_id in zip(roles, context_ids, revision_ids, strict=True)), workspace={"revision": 1}, budget={})
    curators = CuratorCoordinationStage(sessions)
    assert await curators.dispatch(patrol.session_id, observation, curators.scopes(observation)) == 3
    assignments = CuratorAssignmentRepository()
    async with sessions.begin() as session:
        for assignment in await assignments.by_session(session, patrol.session_id):
            assignment = await assignments.transition(session, assignment.assignment_id, "reading", "Curator 正在读取 Context 证据")
            assignment = await assignments.transition(session, assignment.assignment_id, "analyzing", "Curator 正在分析 Context 证据")
            await assignments.transition(session, assignment.assignment_id, "proposed", "Curator proposal 已提交", result_summary=f"{assignment.scope['role']} evidence ready")
            worker = await session.get(LoopWorkerRequest, assignment.worker_request_id, with_for_update=True)
            worker.status = "success"
            worker.result = {"rationale": f"{assignment.scope['role']} evidence ready", "work_specs": []}
            worker.completed_at = datetime.now(UTC)
        round_row = await session.get(LoopRound, snapshot["current_round_id"], with_for_update=True)
        round_row.status = "curated"
    await lifecycle.transition(patrol.session_id, PatrolPhase.PROPOSING, PatrolActivity(summary="正在汇总三个 Curator proposal"))
    return await lifecycle.transition(patrol.session_id, PatrolPhase.AUTHORIZING, PatrolActivity(summary="正在请求 Kernel 授权"))


async def _publish_testing_tool_activity(sessions, loop_id, directive, run, suffix) -> None:
    activity = LoopRunActivityBridge(MemoryStreamBridge(), sessions, loop_id=loop_id, context_id=run.task_id, run_id=run.run_id, correlation_id=directive.correlation_id, anchor_message_id=directive.message_id)
    call_id = f"pytest-{suffix}"
    ai = {"role": "ai", "id": f"ai-{suffix}", "content": "", "tool_calls": [{"id": call_id, "name": "pytest", "args": {}}]}
    activity.publish(run.run_id, _values(directive.message_id, (ai,)))
    activity.publish(run.run_id, _values(directive.message_id, (ai, {"role": "tool", "id": f"tool-{suffix}", "tool_call_id": call_id, "name": "pytest", "status": "success", "content": "18 passed"})))
    await activity.close()


class _RunningRunLauncher:
    def __init__(self, sessions) -> None:
        self._sessions = sessions

    async def __call__(self, directive, _message, _slot_id) -> str:
        run_id = uuid.uuid4().hex
        async with self._sessions.begin() as session:
            context = await session.get(DesktopThread, directive.target_context_id)
            session.add(DesktopRun(run_id=run_id, task_id=directive.target_context_id, agent_id=f"main:{directive.target_context_id}", kind="main", status="running", origin="delegated_patrol", execution_thread_id=context.thread_id, context_revision_id=directive.target_context_revision_id, directive_id=directive.directive_id, loop_id=directive.loop_id, round_id=directive.round_id, action_id=directive.action_id))
        return run_id


async def _round_frontier(sessions, round_id: str) -> str:
    async with sessions() as session:
        return (await session.get(LoopRound, round_id)).frontier_hash


def _values(anchor_message_id: str, messages: tuple[dict, ...]) -> StreamEvent:
    return StreamEvent(id="", event="events", data={"data": {"messages": [{"role": "human", "id": anchor_message_id, "content": "directive"}, *messages]}})
