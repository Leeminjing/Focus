"""本文件对外提供任务记忆长请求、fence 竞争及阻塞恢复的隔离数据库验收。

输入为真实冻结 Loop、短执行政策和可控模型；输出为有效续租、无重复发布、完整消费及同输入显式恢复的断言。
具体工作流为领取后跨租约等待、竞争与取消，再用实际编排建立 recovery wait 并通过服务响应；不以可控模型代替原生验收。
示例：pytest backend/tests/test_loop_memory_execution_recovery.py。
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import os

import pytest
from fastapi import HTTPException
from focus.runtime.runs.usage import ModelUsage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.agent_loop.models import AgentLoop, LoopRound, LoopDelegationGrant
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.round_orchestration import LoopRoundOrchestrator
from backend.app.desktop.agent_loop.schemas import LoopWaitResponseRequest
from backend.app.desktop.agent_loop.task_progress.contracts import ProgressCandidate, SourceAssessment
from backend.app.desktop.agent_loop.task_progress.execution_policy import ProgressExecutionPolicy
from backend.app.desktop.agent_loop.task_progress.models import LoopDecisionInputs, LoopProgressWork
from backend.app.desktop.agent_loop.task_progress.repository import TaskProgressRepository
from backend.app.desktop.agent_loop.task_progress.runtime import TaskProgressRuntime
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest, LoopWaitResponse
from backend.tests.test_round_task_progress import _Checkpointer
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop, app_config_for

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class ControlledMemory:
    context_window_tokens = None
    max_output_tokens = 50
    last_usage = ModelUsage(model_calls=1, input_tokens=17, output_tokens=9)
    last_usage_reported = False

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancellations = 0

    async def invoke(self, schema, system, payload):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancellations += 1
            raise
        self.last_usage_reported = True
        return ProgressCandidate(source_assessments=tuple(SourceAssessment(
            source_key=source["source_key"], disposition="unknown", explanation="明确保留冻结来源的不确定状态")
            for source in payload["task_delta"]["sources"]))


@asynccontextmanager
async def _case(tmp_path, policy):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixture = await _seed_loop(sessions, tmp_path, label="memory-recovery", started_at=datetime.now(UTC))
    model = ControlledMemory()
    runtime = TaskProgressRuntime(sessions, None, model_factory=lambda name: model, policy=policy)
    try:
        observation = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], fixture["round_id"])
        assert observation.task_delta["sources"]
        yield sessions, fixture, observation.decision_inputs_ref, model, runtime
    finally:
        await _stop(fixture["service"], fixture["loop_id"])
        await engine.dispose()


@pytest.mark.parametrize("lose_fence", [False, True])
def test_memory_request_renews_or_cancels_without_duplicate_publication(tmp_path, lose_fence):
    async def run():
        policy = ProgressExecutionPolicy(request_seconds=10, lease_seconds=1, renew_seconds=0.15)
        async with _case(tmp_path, policy) as (sessions, fixture, observation_id, model, runtime):
            task = asyncio.create_task(runtime.drain(loop_id=fixture["loop_id"]))
            try:
                await asyncio.wait_for(model.started.wait(), 10)
                async with sessions() as session:
                    first = await session.get(LoopProgressWork, observation_id)
                    first_expiry, fence = first.lease_expires_at, first.fence
                if lose_fence:
                    async with sessions.begin() as session:
                        work = await session.get(LoopProgressWork, observation_id, with_for_update=True)
                        work.fence += 1
                        work.lease_expires_at = datetime.now(UTC) + timedelta(seconds=10)
                    assert await asyncio.wait_for(task, 10) == 1
                    assert model.cancellations == 1
                else:
                    await asyncio.sleep(1.6)
                    async with sessions() as session:
                        current = await session.get(LoopProgressWork, observation_id)
                        assert current.lease_expires_at > first_expiry
                    assert await TaskProgressRuntime(sessions, None, policy=policy)._claim(loop_id=fixture["loop_id"]) is None
                    model.release.set()
                    assert await asyncio.wait_for(task, 10) == 1
                    assert await runtime.drain(loop_id=fixture["loop_id"]) == 0
                async with sessions() as session:
                    work = await session.get(LoopProgressWork, observation_id)
                    progress = await TaskProgressRepository().current(session, fixture["loop_id"])
                    assert progress.generation == (0 if lose_fence else 1)
                    assert work.state == ("claimed" if lose_fence else "published")
                    assert work.usage[0]["fence"] == fence
                    assert work.usage[0]["accounted"]
                    assert work.usage[0]["usage_reported"] is not lose_fence
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_cancelled_memory_worker_keeps_receipt_and_recovery_fence(tmp_path):
    async def run():
        policy = ProgressExecutionPolicy(request_seconds=10, lease_seconds=1, renew_seconds=0.15)
        async with _case(tmp_path, policy) as (sessions, fixture, observation_id, model, runtime):
            task = asyncio.create_task(runtime.drain(loop_id=fixture["loop_id"]))
            await asyncio.wait_for(model.started.wait(), 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert model.cancellations == 1
            async with sessions() as session:
                work = await session.get(LoopProgressWork, observation_id)
                expiry = work.lease_expires_at
                assert work.state == "claimed" and work.fence == 1
                assert work.usage[0]["accounted"] and not work.usage[0]["usage_reported"]
                assert (await TaskProgressRepository().current(session, fixture["loop_id"])).generation == 0
            await asyncio.sleep(1.2)
            async with sessions() as session:
                assert (await session.get(LoopProgressWork, observation_id)).lease_expires_at == expiry
            model.release.set()
            assert await runtime.drain(loop_id=fixture["loop_id"]) == 1
            async with sessions() as session:
                work = await session.get(LoopProgressWork, observation_id)
                assert work.state == "published" and work.fence == 2
                assert [entry["usage_reported"] for entry in work.usage] == [False, True]
                assert (await TaskProgressRepository().current(session, fixture["loop_id"])).generation == 1
    asyncio.run(run())


@pytest.mark.parametrize("answer", ["retry", "stop", "revoked"])
def test_exhausted_memory_enters_wait_and_explicit_response_is_atomic(tmp_path, answer):
    async def run():
        policy = ProgressExecutionPolicy(request_seconds=0.01, lease_seconds=5, renew_seconds=1)
        async with _case(tmp_path, policy) as (sessions, fixture, observation_id, model, runtime):
            for _ in range(3):
                assert await runtime.drain(loop_id=fixture["loop_id"]) == 1
            assert model.cancellations == 3
            assert await runtime.drain(loop_id=fixture["loop_id"]) == 0
            async with sessions() as session:
                original = (await session.get(LoopDecisionInputs, observation_id)).content_hash
                assert (await session.get(LoopProgressWork, observation_id)).state == "blocked"
            await fixture["service"].control(fixture["loop_id"], "pause")
            await fixture["service"].control(fixture["loop_id"], "resume")
            claim = await LoopCoordinator(sessions).claim_for_loop(fixture["loop_id"], "memory-recovery")
            orchestrator = LoopRoundOrchestrator(sessions, app_config_for("patrol-test", None), LoopKernel(sessions), _Checkpointer())
            assert await orchestrator.process(claim) is None
            assert await orchestrator.process(claim) is None
            request = await fixture["service"].active_wait_request(fixture["loop_id"])
            assert request["scope"]["observation_id"] == observation_id
            assert request["scope"]["failure_kind"] == "TimeoutError"
            assert request["created_by"] == "progress-memory"
            assert request["response_contract"]["actions"][0]["label"] == "重试任务记忆"
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                successor_id = loop.current_round_id
                assert loop.status == "waiting_user"
                assert (await session.get(LoopRound, successor_id)).observation_id is None
                assert await session.scalar(select(func.count()).select_from(LoopWaitRequest).where(LoopWaitRequest.loop_id == loop.loop_id)) == 1
            body = LoopWaitResponseRequest(request_revision=request["revision"], idempotency_key=f"memory:{observation_id}",
                answer={"action": "stop" if answer == "stop" else "retry"})
            if answer == "revoked":
                async with sessions.begin() as session:
                    grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == fixture["loop_id"]))
                    grant.status = "revoked"
                with pytest.raises(HTTPException, match="有效的预算授权"):
                    await fixture["service"].resolve_wait_request(fixture["loop_id"], request["request_id"], body, actor_id="user")
            else:
                result = await fixture["service"].resolve_wait_request(fixture["loop_id"], request["request_id"], body, actor_id="user")
                repeated = await fixture["service"].resolve_wait_request(fixture["loop_id"], request["request_id"], body, actor_id="user")
                assert result["created"] and not repeated["created"]
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                work = await session.get(LoopProgressWork, observation_id)
                assert loop.current_round_id == successor_id
                assert (await session.get(LoopDecisionInputs, observation_id)).content_hash == original
                assert len(work.usage) == 3
                assert work.state == ("pending" if answer == "retry" else "blocked")
                assert loop.status == {"retry": "running", "stop": "stopped", "revoked": "waiting_user"}[answer]
                assert await session.scalar(select(func.count()).select_from(LoopWaitResponse).where(LoopWaitResponse.request_id == request["request_id"])) == (0 if answer == "revoked" else 1)
            if answer == "retry":
                model.release.set()
                assert await runtime.drain(loop_id=fixture["loop_id"]) == 1
                frozen = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], successor_id)
                assert frozen.round_id == successor_id
    asyncio.run(run())
