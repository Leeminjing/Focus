r"""本文件对外提供持久单来源恢复从 Patrol identity 到原子 Portfolio 发布的 PostgreSQL 集成测试。

输入为真实 Loop、provider 400 失败、悬空 Tool Exchange、精确 source revision 与 identity-only Patrol action；输出为持久 recovery
opportunity、Kernel 授权、Lane/Context/Directive 发布、机会消费、严格 Provider 续跑及重放不重复创建的断言。具体工作流为播种 Loop、扩展 grant，先由
recovery service 从失败 Run 和 authored revision 发现并持久化机会，经 ContextRecoveryStage 解析后交给标准 LoopKernel 和
LoopPortfolioPublicationService，最后从新 session 验证消费与幂等状态。示例：
`pytest backend/tests/test_context_recovery_integration.py -q`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.context_recovery import (
    ContextRecoveryOpportunityRepository,
    ContextRecoveryOpportunityService,
    ContextRecoveryStage,
)
from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopContextRecoveryOpportunity,
    LoopDelegationGrant,
    LoopRound,
)
from backend.app.desktop.agent_loop.portfolio_publication import LoopPortfolioPublicationService
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope, PatrolDecisionIntent
from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.context_curation import CurationLane
from backend.app.desktop.context_evolution import ContextRevisionReader, ContextRevisionRepository
from backend.app.desktop.run_orchestration import RunLifecycleFinalizer, RunRegistrar
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_unified_run_orchestration import _registration
from focus.runtime.runs.events import validate_messages
from focus.runtime.runs.manager import RunRecord
from focus.runtime.runs.schemas import DisconnectMode, RunStatus

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _Checkpoint(BaseCheckpointSaver):
    def __init__(self, graph) -> None:
        super().__init__()
        self._graph = graph
        self._continuations = {}

    def set_continuation(self, thread_id, messages):
        self._continuations[thread_id] = tuple(messages)

    async def aget_tuple(self, config):
        configurable = config["configurable"]
        thread_id = configurable.get("thread_id")
        checkpoint_id = configurable.get("checkpoint_id")
        if checkpoint_id and checkpoint_id.startswith("prepared-"):
            messages = self._graph._messages
        elif thread_id in self._continuations and not checkpoint_id:
            checkpoint_id = f"continued-{thread_id}"
            messages = self._continuations[thread_id]
        else:
            messages = [
                HumanMessage(
                    content="Preserve the original goal, permission boundary, and interface contract.",
                    id="source-evidence",
                ),
                AIMessage(
                    content="I will inspect the tests.",
                    id="source-caller",
                    tool_calls=(
                        {
                            "id": "call-1",
                            "name": "read_file",
                            "args": {"path": "tests.py"},
                            "type": "tool_call",
                        },
                    ),
                ),
            ]
        return SimpleNamespace(
            config={"configurable": {"checkpoint_id": checkpoint_id}},
            checkpoint={"channel_values": {"messages": messages}},
            metadata={},
        )


class _Graph:
    def __init__(self) -> None:
        self._messages = []

    async def aupdate_state(self, config, values):
        self._messages = list(values["messages"])
        return {"configurable": {**config["configurable"], "checkpoint_id": f"prepared-{uuid.uuid4().hex}"}}

    async def aget_state(self, config):
        return SimpleNamespace(config=config, values={"messages": self._messages})


class _ContextService:
    def __init__(self) -> None:
        self.graph = _Graph()
        self.checkpointer = _Checkpoint(self.graph)

    async def make_state_graph(self):
        return self.graph


def test_persisted_recovery_is_validated_published_consumed_and_replay_safe(tmp_path: Path) -> None:
    async def run() -> None:
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        context_service = _ContextService()
        seeded = await _seed_loop(sessions, tmp_path, label="single-recovery", started_at=datetime.now(UTC))
        try:
            async with sessions.begin() as session:
                loop_grant = await session.scalar(
                    select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == seeded["loop_id"])
                )
                loop_grant.capabilities = [*loop_grant.capabilities, "create_lane"]
                repository = ContextRevisionRepository()
                source = (await repository.current(session, seeded["context_id"])).ref
                round_row = await session.get(LoopRound, seeded["round_id"])
                loop = await session.get(AgentLoop, seeded["loop_id"])
                discovery = await ContextRecoveryOpportunityService(
                    ContextRevisionReader(repository, context_service.checkpointer)
                ).discover(
                    session,
                    loop=loop,
                    round_row=round_row,
                    grant=loop_grant,
                    workspace_revision=round_row.workspace_revision,
                    frontier=(
                        {
                            "context_id": seeded["context_id"],
                            "revision": source.model_dump(mode="json"),
                        },
                    ),
                    runs=(
                        SimpleNamespace(
                            run_id="failed-run",
                            task_id=seeded["context_id"],
                            status="error",
                            error="provider 400: insufficient tool messages",
                        ),
                    ),
                )
                assert discovery.waiting_reason is None
                assert len(discovery.opportunities) == 1
                opportunity = await ContextRecoveryOpportunityRepository().get_contract(
                    session,
                    discovery.opportunities[0]["opportunity_id"],
                )
                assert opportunity is not None

            async with sessions() as session:
                replayed = await ContextRecoveryOpportunityRepository().pending_for_round(session, seeded["round_id"])
            assert replayed == (opportunity.public_payload(),)

            observation = LoopObservationEnvelope(
                loop_id=seeded["loop_id"],
                loop_revision=seeded["snapshot"]["revision"],
                round_id=seeded["round_id"],
                goal_revision=1,
                authority_revision=1,
                observed_frontier_hash=round_row.frontier_hash,
                mission={"outcome": "Recover and continue", "completion_checks": []},
                grant={"grant_id": loop_grant.grant_id, "revision": 1},
                portfolio_frontier=(),
                workspace={"revision": round_row.workspace_revision},
                budget={},
                recovery_opportunities=(opportunity.public_payload(),),
            )
            raw = PatrolDecisionIntent(
                decision_id=uuid.uuid4().hex,
                idempotency_key=f"recover:{opportunity.opportunity_id}",
                loop_id=seeded["loop_id"],
                loop_revision=seeded["snapshot"]["revision"],
                round_id=seeded["round_id"],
                holder_id=seeded["snapshot"]["holder_id"],
                grant_id=loop_grant.grant_id,
                grant_revision=1,
                goal_revision=1,
                observed_frontier_hash=round_row.frontier_hash,
                observed_workspace_revision=round_row.workspace_revision,
                rationale="Use the persisted recovery opportunity.",
                actions=({"action": "recover_context", "opportunity_id": opportunity.opportunity_id},),
            )
            resolved = await ContextRecoveryStage(sessions).resolve(observation, raw)
            assert resolved.intent is not None
            kernel = LoopKernel(sessions, LoopPortfolioPublicationService(sessions, context_service))

            first = await kernel.commit(resolved.intent)
            replay = await kernel.commit(resolved.intent)

            assert first.status == replay.status == "committed"
            async with sessions() as session:
                stored = await session.get(LoopContextRecoveryOpportunity, opportunity.opportunity_id)
                lane_count = await session.scalar(
                    select(func.count()).select_from(CurationLane).where(CurationLane.program_id == seeded["snapshot"]["program_id"])
                )
            assert stored.status == "consumed"
            assert stored.consumed_by_decision_id == resolved.intent.decision_id
            assert stored.result["context_id"]
            assert int(lane_count or 0) == 2
            recovered_context_id = stored.result["context_id"]
            async with sessions() as session:
                recovered = await ContextRevisionRepository().current(session, recovered_context_id)
                execution = await ContextRevisionReader(
                    ContextRevisionRepository(), context_service.checkpointer
                ).read(session, recovered.ref, "execution")

            async def strict_provider(messages):
                validate_messages(list(messages))
                return AIMessage(id="recovered-provider-response", content="Recovery context can continue.")

            response = await strict_provider(execution.messages)
            assert response.content == "Recovery context can continue."
            continued_run_id = uuid.uuid4().hex
            await RunRegistrar(sessions).register(
                _registration(recovered_context_id, recovered.ref, continued_run_id, "recovery-followup")
            )
            context_service.checkpointer.set_continuation(
                recovered.ref.execution_thread_id,
                (*context_service.graph._messages, response),
            )
            settlement = await RunLifecycleFinalizer(sessions, context_service.checkpointer).finalize(
                RunRecord(
                    run_id=continued_run_id,
                    thread_id=recovered.ref.execution_thread_id,
                    status=RunStatus.success,
                    on_disconnect=DisconnectMode.cancel,
                )
            )
            assert settlement.context_publication == "published"
            assert settlement.context_revision.generation == recovered.ref.generation + 1
            async with sessions() as session:
                assert await ContextRecoveryOpportunityRepository().pending_for_round(session, seeded["round_id"]) == ()
        finally:
            await _stop(seeded["service"], seeded["loop_id"])
            await engine.dispose()

    asyncio.run(run())
