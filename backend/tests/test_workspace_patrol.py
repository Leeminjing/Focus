"""本文件对外提供工作区首次受理、首 Revision、去重与暂停保持的真实 PostgreSQL 回归。

输入为隔离库、临时工作区和无采样 graph writer，输出为真实输入事务及 Context/Portfolio 发布断言。
具体工作流为使用生产受理/发布服务，验证首输入无 Main Run、身份重试及暂停不恢复。
示例：python -m pytest backend/tests/test_workspace_patrol.py -q。
"""

import asyncio
import os
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from focus.agents.lead_agent_state import LeadAgentState
from backend.app.desktop.models import DesktopWorkspace, DesktopThread, DesktopRun
from backend.app.desktop.agent_loop.models import AgentLoop, LoopUserIntent
from backend.app.desktop.agent_loop.patrol_inputs import PatrolInputRequest
from backend.app.desktop.agent_loop.workspace_patrol import (
    WorkspacePatrolService,
    WorkspacePatrolBootstrap,
)
from backend.app.desktop.context_evolution import ContextEvolutionService

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _setup(tmp_path):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace_id = uuid.uuid4().hex
    async with sessions.begin() as session:
        session.add(
            DesktopWorkspace(workspace_id=workspace_id, path=str(tmp_path), display_name="测试")
        )
    desktop = SimpleNamespace(app_config=SimpleNamespace(resolve_default_model_name=lambda: "test"))
    return engine, sessions, workspace_id, WorkspacePatrolService(sessions, desktop)


@pytest.mark.parametrize("input_type", ["information", "outcome", "boundary", "completion_check"])
def test_first_input_without_context_or_run_and_retry(tmp_path, input_type):
    async def run():
        engine, sessions, workspace_id, service = await _setup(tmp_path)
        try:
            request = PatrolInputRequest(
                submission_id=uuid.uuid4().hex, input_type=input_type, content="  我想做本地工具\n"
            )
            accepted = await service.submit(workspace_id, request)
            assert await service.submit(workspace_id, request) == accepted
            async with sessions() as session:
                loop = await session.get(AgentLoop, accepted["loop_id"])
                context = await session.get(DesktopThread, loop.initial_context_id)
                assert context.current_revision_id is None
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(DesktopRun)
                        .where(DesktopRun.loop_id == loop.loop_id)
                    )
                    == 0
                )

            async def graph_factory():
                builder = StateGraph(LeadAgentState)
                builder.add_node("noop", lambda state: {})
                builder.add_edge(START, "noop")
                builder.add_edge("noop", END)
                return builder.compile()

            checkpointer = InMemorySaver()
            bootstrap = WorkspacePatrolBootstrap(
                sessions, ContextEvolutionService(sessions, checkpointer, graph_factory)
            )
            await bootstrap.prepare(accepted["loop_id"])
            await bootstrap.prepare(accepted["loop_id"])
            async with sessions() as session:
                context = await session.get(DesktopThread, context.task_id)
                loop = await session.get(AgentLoop, loop.loop_id)
                assert context.current_revision_id and loop.current_portfolio_revision_id
                source = await session.get(LoopUserIntent, accepted["intent_id"])
                assert source.content == request.content and source.status == "pending"
                assert len((await service.history(workspace_id))["items"]) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_intake_and_paused_does_not_resume(tmp_path):
    async def run():
        engine, sessions, workspace_id, service = await _setup(tmp_path)
        try:
            requests = [
                PatrolInputRequest(submission_id=uuid.uuid4().hex, content="同样原文")
                for _ in range(2)
            ]
            a, b = await asyncio.gather(*(service.submit(workspace_id, body) for body in requests))
            assert a["loop_id"] == b["loop_id"] and a["intent_id"] != b["intent_id"]
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, a["loop_id"])
                loop.status = "paused"
            await service.submit(
                workspace_id,
                PatrolInputRequest(submission_id=uuid.uuid4().hex, content="暂停时补充"),
            )
            async with sessions() as session:
                assert (await session.get(AgentLoop, a["loop_id"])).status == "paused"
            with pytest.raises(HTTPException) as error:
                await service.submit(
                    workspace_id, requests[0].model_copy(update={"content": "改变原文"})
                )
            assert error.value.status_code == 409
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_invalid_workspace_and_history_scope_do_not_start_or_cross(tmp_path):
    from pydantic import ValidationError

    async def run():
        engine, sessions, workspace, service = await _setup(tmp_path)
        try:
            with pytest.raises(ValidationError):
                PatrolInputRequest(submission_id="empty", content="  \n\t")
            with pytest.raises(HTTPException):
                await service.submit(
                    "missing", PatrolInputRequest(submission_id="missing", content="非空")
                )
            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(AgentLoop)
                        .where(AgentLoop.workspace_id == workspace)
                    )
                    == 0
                )
            receipts = [
                await service.submit(
                    workspace,
                    PatrolInputRequest(submission_id=uuid.uuid4().hex, content=str(index)),
                )
                for index in range(5)
            ]
            page = await service.history(workspace, limit=2)
            assert len(page["items"]) == 2 and page["has_more"]
            older = await service.history(workspace, page["next_before"], limit=2)
            assert len(older["items"]) == 2
            assert not {item["intent_id"] for item in page["items"]}.intersection(
                item["intent_id"] for item in older["items"]
            )
            other = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(
                    DesktopWorkspace(
                        workspace_id=other, path=str(tmp_path / "other"), display_name="另一工作区"
                    )
                )
            with pytest.raises(HTTPException):
                await service.history(other, receipts[0]["intent_id"])
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_first_publication_rollback_can_recover_same_thread(tmp_path, monkeypatch):
    async def run():
        from backend.app.desktop.context_evolution.models import ContextRevision

        engine, sessions, workspace, service = await _setup(tmp_path)
        try:
            first = await service.submit(
                workspace, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="初次探索")
            )

            async def graph_factory():
                graph = StateGraph(LeadAgentState)
                graph.add_node("noop", lambda state: {})
                graph.add_edge(START, "noop")
                graph.add_edge("noop", END)
                return graph.compile()

            contexts = ContextEvolutionService(sessions, InMemorySaver(), graph_factory)
            real_stage = contexts.stage_definition

            async def fail_after_prepare(*args, **kwargs):
                await real_stage(*args, **kwargs)
                raise ValueError("首发布事务故障")

            monkeypatch.setattr(contexts, "stage_definition", fail_after_prepare)
            with pytest.raises(ValueError, match="首发布事务故障"):
                await WorkspacePatrolBootstrap(sessions, contexts).prepare(first["loop_id"])
            assert (await service.lineage(first["loop_id"]))["nodes"] == []
            monkeypatch.setattr(contexts, "stage_definition", real_stage)
            await WorkspacePatrolBootstrap(sessions, contexts).prepare(first["loop_id"])
            async with sessions() as session:
                loop = await session.get(AgentLoop, first["loop_id"])
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(ContextRevision)
                        .where(ContextRevision.context_id == loop.initial_context_id)
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(DesktopThread)
                        .where(DesktopThread.workspace_id == workspace)
                    )
                    == 1
                )
            assert len((await service.lineage(first["loop_id"]))["nodes"]) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_real_observation_kernel_input_application_idle_and_wakeup(tmp_path):
    async def run():
        from backend.app.desktop.agent_loop.kernel import LoopKernel
        from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
        from backend.app.desktop.agent_loop.models import LoopRound, LoopDelegationGrant
        from backend.app.desktop.agent_loop.schemas import PatrolDecisionIntent

        engine, sessions, workspace_id, service = await _setup(tmp_path)
        try:
            accepted = await service.submit(
                workspace_id,
                PatrolInputRequest(submission_id=uuid.uuid4().hex, content="先探索需求"),
            )

            async def graph_factory():
                builder = StateGraph(LeadAgentState)
                builder.add_node("noop", lambda state: {})
                builder.add_edge(START, "noop")
                builder.add_edge("noop", END)
                return builder.compile()

            checkpointer = InMemorySaver()
            await WorkspacePatrolBootstrap(
                sessions, ContextEvolutionService(sessions, checkpointer, graph_factory)
            ).prepare(accepted["loop_id"])
            async with sessions() as session:
                loop = await session.get(AgentLoop, accepted["loop_id"])
                grant = await session.scalar(
                    select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id)
                )
            observation = await LoopObservationService(sessions, checkpointer).capture(
                loop.loop_id, loop.current_round_id
            )
            assert (
                observation.interaction_mode == "workspace_patrol"
                and observation.user_intents[0]["content"] == "先探索需求"
            )
            intent = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key=uuid.uuid4().hex,
                loop_id=loop.loop_id,
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
                rationale="信息已经讨论，等待新的决定",
                actions=[
                    {
                        "action": "apply_user_inputs",
                        "inputs": [
                            {
                                "intent_id": accepted["intent_id"],
                                "disposition": "discussion",
                                "explanation": "探索输入",
                            }
                        ],
                    },
                    {"action": "wait_for_user", "cause": "awaiting_input", "reason": "等待新输入"},
                ],
            )
            result = await LoopKernel(sessions).commit(intent)
            assert result.status == "committed", result.reason
            assert (await LoopKernel(sessions).commit(intent)).decision_id == result.decision_id
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop.loop_id)
                assert loop.status == "waiting_user" and loop.waiting_reason == "awaiting_input"
                assert (
                    await session.get(LoopUserIntent, accepted["intent_id"])
                ).status == "addressed"
            from backend.app.desktop.agent_loop.service import AgentLoopService

            controls = AgentLoopService(sessions)
            await controls.control(loop.loop_id, "pause")
            await service.submit(
                workspace_id,
                PatrolInputRequest(submission_id=uuid.uuid4().hex, content="明确暂停时补充"),
            )
            async with sessions() as session:
                assert (await session.get(AgentLoop, loop.loop_id)).status == "paused"
            await controls.control(loop.loop_id, "resume")
            received = await service.submit(
                workspace_id, PatrolInputRequest(submission_id=uuid.uuid4().hex, content="继续探讨")
            )
            async with sessions() as session:
                loop = await session.get(AgentLoop, loop.loop_id)
                assert loop.status == "running" and received["loop_id"] == loop.loop_id
                assert (await session.get(LoopRound, loop.current_round_id)).number == 2
        finally:
            await engine.dispose()

    asyncio.run(run())
