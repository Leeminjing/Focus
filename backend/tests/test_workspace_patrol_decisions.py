"""本文件对外提供用户来源 Mission 分区修订、迟到输入及定向补充的事务回归。

输入为隔离 PostgreSQL、无模型首 Revision 与真实冻结 Observation；输出为生产 Kernel 的拒绝/提交及持久状态断言。
工作流为按轮冻结输入、明确处置、修订 Mission，检查后继输入与开放请求未被批量吞掉。
示例：python -m pytest backend/tests/test_workspace_patrol_decisions.py -q。
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END
from sqlalchemy import select

from focus.agents.lead_agent_state import LeadAgentState
from backend.tests.test_workspace_patrol import _setup
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopDelegationGrant,
    LoopUserIntent,
    LoopRound,
)
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.schemas import PatrolDecisionIntent
from backend.app.desktop.agent_loop.patrol_inputs import PatrolInputRequest
from backend.app.desktop.agent_loop.rounds import create_observation_round
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest, LoopWaitResponse
from backend.app.desktop.agent_loop.workspace_patrol import WorkspacePatrolBootstrap
from backend.app.desktop.context_evolution import ContextEvolutionService

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")
_checkpointers = {}


async def _prepare(sessions, loop_id):
    async def graph_factory():
        graph = StateGraph(LeadAgentState)
        graph.add_node("noop", lambda state: {})
        graph.add_edge(START, "noop")
        graph.add_edge("noop", END)
        return graph.compile()

    checkpointer = InMemorySaver()
    _checkpointers[loop_id] = checkpointer
    await WorkspacePatrolBootstrap(
        sessions, ContextEvolutionService(sessions, checkpointer, graph_factory)
    ).prepare(loop_id)


async def _decision(sessions, loop_id, actions):
    from backend.app.desktop.agent_loop.task_progress.models import LoopProgressWork
    from backend.tests.test_loop_round_progress_admission import _publish_memory

    async with sessions() as session:
        unfinished = await session.scalar(
            select(LoopProgressWork).where(
                LoopProgressWork.loop_id == loop_id, LoopProgressWork.state == "pending"
            )
        )
    if unfinished:
        await _publish_memory(sessions, loop_id)
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, loop_id)
        current = await session.get(LoopRound, loop.current_round_id)
        if current.status == "settled":
            successor = await create_observation_round(
                session, loop, current, current.frontier_hash
            )
            session.add(successor)
            loop.current_round_id = successor.round_id
    observation = await LoopObservationService(sessions, _checkpointers[loop_id]).capture(
        loop_id, loop.current_round_id
    )
    async with sessions() as session:
        loop = await session.get(AgentLoop, loop_id)
        grant = await session.scalar(
            select(LoopDelegationGrant).where(
                LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active"
            )
        )
        intent = PatrolDecisionIntent(
            decision_id=uuid.uuid4().hex,
            idempotency_key=uuid.uuid4().hex,
            loop_id=loop_id,
            loop_revision=loop.revision,
            round_id=loop.current_round_id,
            holder_id=loop.holder_id,
            grant_id=grant.grant_id,
            grant_revision=grant.revision,
            goal_revision=loop.goal_revision,
            observed_frontier_hash=observation.observed_frontier_hash,
            observed_workspace_revision=observation.workspace["revision"],
            observed_projection_sequence=observation.projection_sequence,
            base_entity_revisions=observation.base_entity_revisions,
            rationale="处理冻结用户来源",
            actions=actions,
        )
    return intent


def _use(row, disposition="decision"):
    return {
        "intent_id": row["intent_id"],
        "disposition": disposition,
        "explanation": "明确处理此输入",
    }


def test_mission_partitions_and_late_input_are_exact(tmp_path):
    async def run():
        engine, sessions, workspace, service = await _setup(tmp_path)
        try:
            first = await service.submit(
                workspace,
                PatrolInputRequest(
                    submission_id=uuid.uuid4().hex,
                    input_type="boundary",
                    content="不要访问真实 Vault",
                ),
            )
            await _prepare(sessions, first["loop_id"])
            actions = [
                {
                    "action": "apply_user_inputs",
                    "inputs": [_use(first)],
                    "changes": [
                        {
                            "intent_id": first["intent_id"],
                            "section": "boundary",
                            "quote": first["content"],
                            "boundary_group": "prohibited_actions",
                        }
                    ],
                }
            ]
            intent = await _decision(sessions, first["loop_id"], actions)
            late = await service.submit(
                workspace,
                PatrolInputRequest(
                    submission_id=uuid.uuid4().hex, input_type="outcome", content="做本地保存"
                ),
            )
            result = await LoopKernel(sessions).commit(intent)
            assert result.status == "committed", result.reason
            async with sessions() as session:
                loop = await session.get(AgentLoop, first["loop_id"])
                mission = await session.scalar(
                    select(LoopMissionRevision).where(
                        LoopMissionRevision.loop_id == loop.loop_id,
                        LoopMissionRevision.revision == loop.goal_revision,
                    )
                )
                assert mission.outcome is None and mission.completion_checks == []
                assert mission.boundaries["prohibited_actions"] == [first["content"]]
                assert (await session.get(LoopUserIntent, late["intent_id"])).status == "pending"
            intent = await _decision(
                sessions,
                first["loop_id"],
                [
                    {
                        "action": "apply_user_inputs",
                        "inputs": [_use(late)],
                        "changes": [
                            {
                                "intent_id": late["intent_id"],
                                "section": "outcome",
                                "quote": late["content"],
                            }
                        ],
                    }
                ],
            )
            assert (await LoopKernel(sessions).commit(intent)).status == "committed"
            async with sessions() as session:
                mission = await session.scalar(
                    select(LoopMissionRevision)
                    .where(LoopMissionRevision.loop_id == first["loop_id"])
                    .order_by(LoopMissionRevision.revision.desc())
                )
                assert mission.outcome == late["content"]
                assert mission.boundaries["prohibited_actions"] == [first["content"]]
                assert mission.input_sources["outcome"][0]["intent_id"] == late["intent_id"]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_information_request_survives_proactive_input_and_targeted_answer(tmp_path):
    async def run():
        engine, sessions, workspace, service = await _setup(tmp_path)
        try:
            first = await service.submit(
                workspace,
                PatrolInputRequest(submission_id=uuid.uuid4().hex, content="帮我选择登录方式"),
            )
            await _prepare(sessions, first["loop_id"])
            actions = [
                {"action": "apply_user_inputs", "inputs": [_use(first, "discussion")]},
                {
                    "action": "wait_for_user",
                    "cause": "missing_input",
                    "reason": "需要选择",
                    "required_input": "使用密码还是验证码？",
                    "evidence_identity": {"kind": "input", "reference_id": first["intent_id"]},
                },
            ]
            result = await LoopKernel(sessions).commit(
                await _decision(sessions, first["loop_id"], actions)
            )
            assert result.status == "committed", result.reason
            async with sessions() as session:
                loop = await session.get(AgentLoop, first["loop_id"])
                request = await session.scalar(
                    select(LoopWaitRequest).where(
                        LoopWaitRequest.loop_id == loop.loop_id, LoopWaitRequest.status == "open"
                    )
                )
                assert loop.waiting_reason == "awaiting_input" and request.scope["information_only"]
            proactive = await service.submit(
                workspace,
                PatrolInputRequest(submission_id=uuid.uuid4().hex, content="先检查工作区"),
            )
            async with sessions() as session:
                assert (await session.get(LoopWaitRequest, request.request_id)).status == "open"
                assert (
                    await session.scalar(
                        select(LoopWaitResponse).where(
                            LoopWaitResponse.request_id == request.request_id
                        )
                    )
                    is None
                )
            body = PatrolInputRequest(
                submission_id=uuid.uuid4().hex,
                content="用验证码",
                request_id=request.request_id,
                request_revision=request.revision,
            )
            answer = await service.submit(workspace, body)
            assert await service.submit(workspace, body) == answer
            with pytest.raises(HTTPException):
                await service.submit(
                    workspace, body.model_copy(update={"submission_id": uuid.uuid4().hex})
                )
            intent = await _decision(
                sessions,
                first["loop_id"],
                [
                    {
                        "action": "apply_user_inputs",
                        "inputs": [_use(answer)],
                        "resolved_request_ids": [request.request_id],
                    }
                ],
            )
            result = await LoopKernel(sessions).commit(intent)
            assert result.status == "committed", result.reason
            async with sessions() as session:
                assert (await session.get(LoopWaitRequest, request.request_id)).status == "resolved"
                assert (
                    await session.get(LoopUserIntent, proactive["intent_id"])
                ).status == "observed"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_explicit_successor_preserves_context_progress_grant_and_old_inputs(tmp_path):
    async def run():
        from backend.app.desktop.agent_loop.terminal_lifecycle import LoopTerminalLifecycle
        from backend.app.desktop.agent_loop.task_progress.repository import TaskProgressRepository
        from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
        from backend.app.desktop.agent_loop.models import LoopContextMembership

        engine, sessions, workspace, service = await _setup(tmp_path)
        try:
            first = await service.submit(
                workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="继续探索")
            )
            await _prepare(sessions, first["loop_id"])
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, first["loop_id"])
                prior_context = loop.initial_context_id
                progress = await TaskProgressRepository().current(session, loop.loop_id)
                member = await session.scalar(
                    select(LoopContextMembership).where(
                        LoopContextMembership.loop_id == loop.loop_id
                    )
                )
                retained = WorkspaceSlot(
                    slot_id=uuid.uuid4().hex,
                    workspace_id=workspace,
                    kind="isolated",
                    root_path=str(tmp_path / "retained"),
                    current_fingerprint="f" * 64,
                    owner_loop_id=loop.loop_id,
                    owner_lane_id=member.lane_id,
                    lifecycle="retained",
                )
                session.add(retained)
                await LoopTerminalLifecycle().finalize(session, loop, "stopped", "user_stop")
            late = await service.submit(
                workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="停止时补充")
            )
            assert late["loop_id"] == first["loop_id"]
            successor = await service.restart(workspace)
            assert successor["loop_id"] != first["loop_id"]
            assert await service.restart(workspace) == successor
            async with sessions() as session:
                current = await session.get(AgentLoop, successor["loop_id"])
                old = await session.get(AgentLoop, first["loop_id"])
                assert current.initial_context_id == prior_context and old.status == "stopped"
                assert current.current_portfolio_revision_id
                assert (
                    await TaskProgressRepository().current(session, current.loop_id)
                ).document == progress.document
                grant = await session.scalar(
                    select(LoopDelegationGrant).where(
                        LoopDelegationGrant.loop_id == current.loop_id
                    )
                )
                assert grant.permission_scope == ["read", "write", "host_command"]
                inherited_slot = await session.get(WorkspaceSlot, retained.slot_id)
                assert (
                    inherited_slot.owner_loop_id == current.loop_id
                    and inherited_slot.lifecycle == "retained"
                )
                assert (
                    inherited_slot.revision == 1 and inherited_slot.current_fingerprint == "f" * 64
                )
            _checkpointers[successor["loop_id"]] = _checkpointers[first["loop_id"]]
            intent = await _decision(
                sessions,
                successor["loop_id"],
                [
                    {
                        "action": "apply_user_inputs",
                        "inputs": [_use(first, "discussion"), _use(late, "discussion")],
                    },
                    {"action": "wait_for_user", "cause": "awaiting_input", "reason": "等待新决定"},
                ],
            )
            result = await LoopKernel(sessions).commit(intent)
            assert result.status == "committed", result.reason
            async with sessions() as session:
                assert (await session.get(LoopUserIntent, first["intent_id"])).loop_id == first[
                    "loop_id"
                ]
                assert (await session.get(LoopUserIntent, late["intent_id"])).status == "addressed"
            assert len((await service.history(workspace))["items"]) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_first_input_through_real_run_settlement_and_views(tmp_path, runtime_postgres_database):
    async def run():
        from types import SimpleNamespace
        from langchain_core.messages import HumanMessage, AIMessage
        from backend.app.desktop.agent_loop.dispatch import (
            LoopWaveDispatcher,
            LoopRunWorkspaceBinder,
        )
        from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
        from backend.app.desktop.agent_loop.fact_projector import FactProjector
        from backend.app.desktop.agent_loop.live_api import LoopLiveSnapshotService
        from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
        from backend.app.desktop.run_orchestration.outbox import RunOutboxConsumer
        from backend.tests.test_loop_execution_ownership import _admit_run
        from backend.tests.loop_run_boundary_support import start_loop_run
        from backend.app.desktop.models import DesktopThread, DesktopRun

        engine, sessions, workspace, service = await _setup(tmp_path)
        try:
            first = await service.submit(
                workspace,
                PatrolInputRequest(
                    submission_id=uuid.uuid4().hex,
                    content="检查本地工作区",
                    access_mode="read-only",
                ),
            )
            await _prepare(sessions, first["loop_id"])
            async with sessions() as session:
                loop = await session.get(AgentLoop, first["loop_id"])
                context = await session.get(DesktopThread, loop.initial_context_id)
            intent = await _decision(
                sessions,
                loop.loop_id,
                [
                    {"action": "apply_user_inputs", "inputs": [_use(first, "discussion")]},
                    {
                        "action": "continue_context",
                        "context_id": context.task_id,
                        "context_revision_id": context.current_revision_id,
                        "message": "只检查工作区并报告结果",
                    },
                ],
            )
            committed = await LoopKernel(sessions).commit(intent)
            assert committed.status == "committed", committed.reason
            fixture = {
                "context_id": context.task_id,
                "revision_id": context.current_revision_id,
                "loop_id": loop.loop_id,
                "round_id": loop.current_round_id,
            }

            async def launch(directive, message, slot):
                return await _admit_run(sessions, fixture, directive.directive_id)

            run_ids = await LoopWaveDispatcher(sessions, launch).dispatch(
                loop.loop_id, loop.current_round_id, 1
            )
            assert len(run_ids) == 1
            run_id = run_ids[0]
            await LoopRunWorkspaceBinder(sessions).bind(
                run_id=run_id, loop_id=loop.loop_id, body=SimpleNamespace(context={})
            )
            await start_loop_run(sessions, run_id)
            checkpointer = _checkpointers[loop.loop_id]
            graph = StateGraph(LeadAgentState)
            graph.add_node("noop", lambda state: {})
            graph.add_edge(START, "noop")
            graph.add_edge("noop", END)
            async with sessions() as session:
                run_row = await session.get(DesktopRun, run_id)
            await graph.compile(checkpointer=checkpointer).ainvoke(
                {
                    "messages": [
                        HumanMessage(id="run-input", content="检查本地工作区"),
                        AIMessage(id="run-result", content="工作区检查结束"),
                    ]
                },
                {"configurable": {"thread_id": run_row.execution_thread_id}},
                durability="sync",
            )
            record = SimpleNamespace(
                run_id=run_id,
                status=SimpleNamespace(value="success"),
                error=None,
                model_call_count=0,
                prompt_input_tokens=0,
                prompt_output_tokens=0,
                prompt_cache_hit_tokens=0,
            )
            finalizer = RunLifecycleFinalizer(sessions, checkpointer)
            settled = await finalizer.finalize(record)
            assert settled.context_revision is not None
            assert (await finalizer.finalize(record)).idempotent
            coordinator = LoopCoordinator(sessions)
            assert (
                await RunOutboxConsumer(sessions).drain(
                    "patrol-test", coordinator.handle_run_settled, loop_id=loop.loop_id
                )
                == 1
            )
            await FactProjector(sessions, checkpointer).project_loop(loop.loop_id)
            async with sessions() as session:
                snapshot = await LoopLiveSnapshotService().read(session, loop.loop_id)
                assert snapshot["runs"][run_id]["state"]["status"] == "success"
                assert snapshot["facts"] and all(
                    item["state"].get("kind") != "tool" for item in snapshot["facts"].values()
                )
            lineage = await service.lineage(loop.loop_id)
            assert lineage["complete"] and len(lineage["nodes"]) >= 2
            from backend.tests.test_loop_round_progress_admission import _publish_memory
            from backend.app.desktop.agent_loop.task_progress.query import TaskProgressQuery

            await _publish_memory(sessions, loop.loop_id)
            async with sessions() as session:
                progress = await TaskProgressQuery().read(session, loop.loop_id)
                assert progress["current"]["generation"] == 1
            late = await service.submit(
                workspace,
                PatrolInputRequest(submission_id=uuid.uuid4().hex, content="继续讨论验收"),
            )
            assert late["loop_id"] == loop.loop_id
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["quote", "partition", "late", "rollback"])
def test_rejected_application_preserves_mission_and_input(tmp_path, monkeypatch, failure):
    async def run():
        from backend.app.desktop.agent_loop.mission_service import MissionRevisionService

        engine, sessions, workspace, service = await _setup(tmp_path)
        try:
            first = await service.submit(
                workspace,
                PatrolInputRequest(
                    submission_id=uuid.uuid4().hex,
                    input_type="boundary",
                    content="禁止访问真实数据",
                ),
            )
            await _prepare(sessions, first["loop_id"])
            change = {
                "intent_id": first["intent_id"],
                "section": "boundary",
                "boundary_group": "prohibited_actions",
                "quote": first["content"],
            }
            if failure == "quote":
                change["quote"] = "模型发明的边界"
            if failure == "partition":
                change["section"] = "outcome"
            intent = await _decision(
                sessions,
                first["loop_id"],
                [{"action": "apply_user_inputs", "inputs": [_use(first)], "changes": [change]}],
            )
            if failure == "late":
                late = await service.submit(
                    workspace,
                    PatrolInputRequest(
                        submission_id=uuid.uuid4().hex, input_type="boundary", content="另一条边界"
                    ),
                )
                action = intent.actions[0].model_copy(
                    update={
                        "inputs": (
                            intent.actions[0]
                            .inputs[0]
                            .model_copy(update={"intent_id": late["intent_id"]}),
                        )
                    }
                )
                intent = intent.model_copy(update={"actions": (action,)})
            if failure == "rollback":

                async def reject(*args, **kwargs):
                    raise ValueError("测试事务发布失败")

                monkeypatch.setattr(MissionRevisionService, "record", reject)
            result = await LoopKernel(sessions).commit(intent)
            assert result.status == "rejected", result.reason
            async with sessions() as session:
                loop = await session.get(AgentLoop, first["loop_id"])
                assert loop.goal_revision == 1
                assert (await session.get(LoopUserIntent, first["intent_id"])).status == "observed"
                rows = tuple(
                    await session.scalars(
                        select(LoopMissionRevision).where(
                            LoopMissionRevision.loop_id == loop.loop_id
                        )
                    )
                )
                assert len(rows) == 1 and rows[0].outcome is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_information_card_is_independent_of_safety_wait_and_pause(tmp_path):
    async def run():
        from backend.app.desktop.agent_loop.wait_requests import (
            LoopWaitRequestFactory,
            LoopWaitRequestService,
        )
        from backend.app.desktop.agent_loop.service import AgentLoopService

        engine, sessions, workspace, service = await _setup(tmp_path)
        try:
            first = await service.submit(
                workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="讨论登录")
            )
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, first["loop_id"], with_for_update=True)
                waits = LoopWaitRequestService()
                information = await waits.open(
                    session,
                    loop,
                    LoopWaitRequestFactory.clarification(
                        "密码或验证码？", {"cause": "missing_input"}
                    ),
                    created_by="test",
                    correlation_id="info",
                )
                assert loop.status == "running"
                safety = await waits.open(
                    session,
                    loop,
                    LoopWaitRequestFactory.retry_or_stop("暂时的准备故障"),
                    created_by="test",
                    correlation_id="recovery",
                )
                assert loop.status == "waiting_user"
                assert (await waits.active(session, loop.loop_id)).request_id == safety.request_id
                assert information.status == "open"
            body = PatrolInputRequest(
                submission_id=uuid.uuid4().hex,
                content="验证码",
                request_id=information.request_id,
                request_revision=information.revision,
            )
            await service.submit(workspace, body)
            async with sessions() as session:
                assert (await session.get(AgentLoop, first["loop_id"])).status == "waiting_user"
                assert (await session.get(LoopWaitRequest, information.request_id)).status == "open"
                assert (await session.get(LoopWaitRequest, safety.request_id)).status == "open"
            await AgentLoopService(sessions).control(first["loop_id"], "stop")
            async with sessions() as session:
                assert (
                    await session.get(LoopWaitRequest, information.request_id)
                ).status == "cancelled"
                assert (await session.get(LoopWaitRequest, safety.request_id)).status == "cancelled"
        finally:
            await engine.dispose()

    asyncio.run(run())
