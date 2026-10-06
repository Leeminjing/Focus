"""本文件对外提供 Worker 显式恢复、冻结输入和旧尝试 fencing 的生产服务回归。

输入为隔离 PostgreSQL、真实 Loop/Observation、WorkerRuntime 和仅替换远端模型响应的 Provider；输出为
三次失败后的原子三次重试批次、消费保留、旧成功/失败/取消拒绝、授权竞争回滚和重启等待幂等断言。
具体工作流为实际领取和模型调用形成错误，再通过等待响应恢复；不直接把 error 工作改为 pending。
示例：pytest backend/tests/test_loop_worker_recovery.py；不访问真实 Vault。
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
import os
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
from langchain_core.messages import AIMessage
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.completion import CompletionEvidenceService
from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDelegationGrant, LoopRound, LoopWorkerRequest
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.recovery import AgentLoopRecovery
from backend.app.desktop.agent_loop.schemas import CompletionVerificationContract, LoopWaitResponseRequest
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest, LoopWaitResponse
from backend.app.desktop.agent_loop.worker_attempts import WorkerAttemptRejected
from backend.app.desktop.agent_loop.workers import LoopWorkerRuntime
from backend.tests.config_helpers import app_config_for
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_round_task_progress import _Checkpointer
from focus.history import content_hash

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class Provider:
    def __init__(self):
        self.valid = False
        self.inputs = []

    async def ainvoke(self, messages, config):
        self.inputs.append(messages[-1].content)
        config["callbacks"][0].usage_metadata["worker-retry"] = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
        criterion = {"check_id": "tests", "status": "unknown", "explanation": "尚无通过证据"}
        if not self.valid:
            criterion["evidence"] = [{"kind": "workspace", "source_id": "wrong-kind", "summary": "禁止替代声明的证据类型"}]
        return AIMessage(content=json.dumps({"criteria": [criterion], "conclusion": "unknown"}))


@asynccontextmanager
async def _case(tmp_path, monkeypatch):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixture = await _seed_loop(sessions, tmp_path, label="worker-retry", started_at=datetime.now(UTC))
    config = app_config_for("worker-retry", None)
    config.models[0].curation_output_method = "prompt_json"
    provider = Provider()
    monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", lambda **kwargs: provider)
    runtime = LoopWorkerRuntime(sessions, config)
    identity = uuid.uuid4().hex
    observation = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], fixture["round_id"])
    async with sessions.begin() as session:
        session.add(LoopWorkerRequest(worker_request_id=identity, loop_id=fixture["loop_id"],
            round_id=fixture["round_id"], kind="completion_verifier", scope={}))
    try:
        yield sessions, fixture, runtime, provider, identity, observation
    finally:
        await runtime.close()
        await _stop(fixture["service"], fixture["loop_id"])
        await engine.dispose()


async def _step(runtime, loop_id):
    assert await runtime.drain(loop_id) == 1
    await asyncio.wait_for(asyncio.gather(*runtime._tasks.values()), 10)


async def _fail_three(runtime, fixture):
    for _ in range(3):
        await _step(runtime, fixture["loop_id"])
    wait = await fixture["service"].active_wait_request(fixture["loop_id"])
    assert wait and wait["kind"] == "recovery_action"
    return wait


@pytest.mark.parametrize("invalid", ["evidence_kind", "unresolved", "duplicate_check"])
def test_verifier_feedback_corrects_contract_without_refreezing_input(tmp_path, monkeypatch, invalid):
    async def run():
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            original_invoke = provider.ainvoke

            async def correct_after_feedback(messages, config):
                provider.valid = bool(provider.inputs)
                result = await original_invoke(messages, config)
                if len(provider.inputs) == 1 and invalid != "evidence_kind":
                    data = json.loads(result.content)
                    if invalid == "unresolved":
                        data["unresolved"] = ["尚缺完整测试结果"]
                    else:
                        data["criteria"] *= 2
                    result = AIMessage(content=json.dumps(data))
                return result

            provider.ainvoke = correct_after_feedback
            await _step(runtime, fixture["loop_id"])
            assert len(provider.inputs) == 2
            feedback = json.loads(provider.inputs[1].split("<worker_input>", 1)[1].split("</worker_input>", 1)[0])["previous_attempt_failure"]
            assert {"evidence_kind": "workspace", "unresolved": "unresolved", "duplicate_check": "唯一"}[invalid] in feedback["message"]
            async with sessions() as session:
                row = await session.get(LoopWorkerRequest, identity)
                assert (row.status, row.attempt) == ("success", 1)
                assert "previous_attempt_failure" not in row.scope["frozen_worker_input"]
                receipts = tuple(await session.scalars(select(LoopIndexBudgetReservation).where(LoopIndexBudgetReservation.loop_id == fixture["loop_id"])))
                assert len(receipts) == 2 and len({r.actual_usage["retry_identity"] for r in receipts}) == 1
                assert all(r.settled_at for r in receipts)
    asyncio.run(run())


def _answer(wait):
    return LoopWaitResponseRequest(request_revision=wait["revision"], idempotency_key=f"worker:{wait['request_id']}", answer={"action": "retry"})


def test_real_verifier_retry_requeues_once_preserves_frozen_input_and_receipts(tmp_path, monkeypatch):
    async def run():
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            wait = await _fail_three(runtime, fixture)
            async with sessions() as session:
                row = await session.get(LoopWorkerRequest, identity)
                original = content_hash(row.scope["frozen_worker_input"])
                assert (row.attempt, row.max_attempts, row.status) == (3, 3, "error")
                assert len(row.scope["attempt_history"]) == 3
            first = await fixture["service"].resolve_wait_request(fixture["loop_id"], wait["request_id"], _answer(wait), actor_id="user")
            repeated = await fixture["service"].resolve_wait_request(fixture["loop_id"], wait["request_id"], _answer(wait), actor_id="user")
            assert first["created"] and not repeated["created"]
            async with sessions() as session:
                row = await session.get(LoopWorkerRequest, identity)
                assert (row.attempt, row.max_attempts, row.status) == (4, 6, "pending")
                assert content_hash(row.scope["frozen_worker_input"]) == original
                loop = await session.get(AgentLoop, fixture["loop_id"])
                assert loop.current_round_id == observation.round_id
                assert (await session.get(LoopRound, loop.current_round_id)).observation_id == observation.decision_inputs_ref
                assert await session.scalar(select(func.count()).select_from(LoopWaitResponse).where(LoopWaitResponse.request_id == wait["request_id"])) == 1
            provider.valid = True
            await _step(runtime, fixture["loop_id"])
            assert len(provider.inputs) == 7
            assert len(set(provider.inputs[::2])) == 1
            feedback = json.loads(provider.inputs[1].split("<worker_input>", 1)[1].split("</worker_input>", 1)[0])["previous_attempt_failure"]
            assert "workspace" in feedback["message"]
            async with sessions() as session:
                row = await session.get(LoopWorkerRequest, identity)
                assert row.status == "success" and len(row.scope["attempt_history"]) == 4
                receipts = tuple(await session.scalars(select(LoopIndexBudgetReservation).where(LoopIndexBudgetReservation.loop_id == fixture["loop_id"])))
                assert len(receipts) == 7
                assert len({r.actual_usage["retry_identity"] for r in receipts}) == 4
                assert all(r.settled_at and r.actual_usage["owner_id"] == identity for r in receipts)
                usage = await session.get(LoopBudgetUsage, fixture["loop_id"])
                assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (7, 140, 70)
    asyncio.run(run())


@pytest.mark.parametrize("change", ["mission", "stop", "revoke", "supersede", "scope", "budget"])
def test_retry_rejects_control_or_scope_change_and_rolls_back_response(tmp_path, monkeypatch, change):
    async def run():
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            wait = await _fail_three(runtime, fixture)
            if change == "stop":
                await fixture["service"].control(fixture["loop_id"], "stop")
            elif change == "mission":
                await fixture["service"].override(fixture["loop_id"], "当前授权 Mission 已修改", "保持模块边界",
                    [{"criterion_id": "tests", "text": "实际测试通过", "required": True}])
            else:
                async with sessions.begin() as session:
                    loop = await session.get(AgentLoop, fixture["loop_id"], with_for_update=True)
                    grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == "active"))
                    if change == "revoke":
                        grant.status = "revoked"
                    elif change == "budget":
                        grant.budgets = {**grant.budgets, "max_model_calls": 3}
                    elif change == "supersede":
                        (await session.get(LoopRound, observation.round_id)).status = "superseded"
                    else:
                        request = await session.get(LoopWaitRequest, wait["request_id"])
                        request.scope = {**request.scope, "worker_kind": "lane_curator"}
            with pytest.raises(HTTPException) as rejected:
                await fixture["service"].resolve_wait_request(fixture["loop_id"], wait["request_id"], _answer(wait), actor_id="user")
            assert rejected.value.status_code == 409
            async with sessions() as session:
                row = await session.get(LoopWorkerRequest, identity)
                assert (row.status, row.attempt, row.max_attempts) == ("error", 3, 3)
                assert await session.scalar(select(func.count()).select_from(LoopWaitResponse).where(LoopWaitResponse.request_id == wait["request_id"])) == 0
            assert len(provider.inputs) == 6
    asyncio.run(run())


def test_late_success_failure_cancel_and_model_reservation_cannot_mutate_new_attempt(tmp_path, monkeypatch):
    async def run():
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            old = (await runtime._claim_many(1, fixture["loop_id"]))[0]
            await runtime._fail(old, RuntimeError("first failure"))
            current = (await runtime._claim_many(1, fixture["loop_id"]))[0]
            assert old.retry_identity != current.retry_identity
            await runtime._fail(old, RuntimeError("late old failure"))
            await runtime._cancel(old, "late old cancellation")
            contract = CompletionVerificationContract(verification_id=uuid.uuid4().hex, loop_id=fixture["loop_id"],
                round_id=fixture["round_id"], goal_revision=1, frontier_hash=observation.observed_frontier_hash,
                workspace_revision=observation.workspace["revision"], criteria=({"check_id": "tests", "status": "unknown", "explanation": "无证据"},), conclusion="unknown")
            with pytest.raises(ValueError):
                await CompletionEvidenceService(sessions).record(contract, identity, retry_identity=old.retry_identity)
            with pytest.raises(WorkerAttemptRejected):
                await runtime._advance(old.loop_id, old.round_id, request=old)
            from backend.app.desktop.agent_loop.model_usage_owner import OwnedModelUsage

            with pytest.raises(ValueError):
                await OwnedModelUsage(sessions, old.loop_id, old.round_id, "worker", identity, retry_identity=old.retry_identity).reserve(20, 10)
            async with sessions() as session:
                row = await session.get(LoopWorkerRequest, identity)
                assert (row.status, row.attempt, row.retry_identity) == ("running", 2, current.retry_identity)
                assert len(row.scope["attempt_history"]) == 1
    asyncio.run(run())


def test_restart_reopens_half_resolved_worker_wait_once_and_keeps_paused_loop(tmp_path, monkeypatch):
    async def run():
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            wait = await _fail_three(runtime, fixture)
            from backend.app.desktop.agent_loop.wait_requests import LoopWaitRequestService

            async with sessions.begin() as session:
                await LoopWaitRequestService().resolve(session, wait["request_id"], answer={"action": "retry"}, actor_id="legacy-user",
                    request_revision=wait["revision"], idempotency_key=f"legacy:{wait['request_id']}")
            async def empty(*args):
                return 0
            async def release(*args):
                return None
            from backend.app.desktop.agent_loop.rounds import RoundStallLimits

            recovery = AgentLoopRecovery(sessions, SimpleNamespace(recover=empty, release_rounds=release, stall_limits=RoundStallLimits()), SimpleNamespace(recover=empty))
            await recovery.reconcile()
            await recovery.reconcile()
            reopened = await fixture["service"].active_wait_request(fixture["loop_id"])
            assert reopened and reopened["request_id"] != wait["request_id"] and reopened["scope"]["worker_request_id"] == identity
            assert (await fixture["service"].get(fixture["loop_id"]))["status"] == "waiting_user"
            await fixture["service"].resolve_wait_request(fixture["loop_id"], reopened["request_id"], _answer(reopened), actor_id="user")
            await fixture["service"].control(fixture["loop_id"], "pause")
            await recovery.reconcile()
            assert (await fixture["service"].get(fixture["loop_id"]))["status"] == "paused"
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["completion_verifier", "lane_curator", "claim_verifier"])
def test_actual_old_model_result_and_task_cleanup_leave_replacement_running(tmp_path, monkeypatch, kind):
    async def run():
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            async with sessions.begin() as session:
                (await session.get(LoopWorkerRequest, identity)).kind = kind
            started = [asyncio.Event(), asyncio.Event()]
            release = [asyncio.Event(), asyncio.Event()]
            invocations = []

            class DelayedProvider:
                async def ainvoke(self, messages, config):
                    index = len(invocations)
                    invocations.append(messages)
                    started[index].set()
                    await release[index].wait()
                    config["callbacks"][0].usage_metadata["worker-retry"] = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
                    result = {"completion_verifier": {"criteria": [{"check_id": "tests", "status": "unknown", "explanation": "无通过证据"}], "conclusion": "unknown"},
                        "lane_curator": {"rationale": "原 Context 范围保持", "work_specs": []}, "claim_verifier": {"assessments": []}}[kind]
                    return AIMessage(content=json.dumps(result))

            monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", lambda **kwargs: DelayedProvider())
            assert await runtime.drain(fixture["loop_id"]) == 1
            await asyncio.wait_for(started[0].wait(), 10)
            async with sessions() as session:
                old = await session.get(LoopWorkerRequest, identity)
            await runtime._fail(old, RuntimeError("replacement authorized after old attempt failure"))
            assert await runtime.drain(fixture["loop_id"]) == 1
            await asyncio.wait_for(started[1].wait(), 10)
            async with sessions() as session:
                current = await session.get(LoopWorkerRequest, identity)
            release[0].set()
            await asyncio.wait_for(runtime._tasks[old.retry_identity], 10)
            runtime._reap()
            assert old.retry_identity not in runtime._tasks
            assert current.retry_identity in runtime._tasks and not runtime._tasks[current.retry_identity].done()
            async with sessions() as session:
                row = await session.get(LoopWorkerRequest, identity)
                assert row.status == "running" and row.retry_identity == current.retry_identity
            release[1].set()
            await asyncio.wait_for(runtime._tasks[current.retry_identity], 10)
            async with sessions() as session:
                row = await session.get(LoopWorkerRequest, identity)
                assert row.status == "success" and row.retry_identity == current.retry_identity
                receipts = tuple(await session.scalars(select(LoopIndexBudgetReservation).where(LoopIndexBudgetReservation.loop_id == fixture["loop_id"])))
                assert len(receipts) == 2 and all(r.settled_at for r in receipts)
                assert {r.actual_usage["retry_identity"] for r in receipts} == {old.retry_identity, current.retry_identity}
    asyncio.run(run())


@pytest.mark.parametrize("command", ["stop", "revoke"])
def test_real_control_retry_race_never_restarts_revoked_worker(tmp_path, monkeypatch, command):
    async def run():
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            wait = await _fail_three(runtime, fixture)
            from backend.app.desktop.agent_loop.authority_control import LoopAuthorityService
            from backend.app.desktop.agent_loop.schemas import RevokeLoopGrantRequest

            async def control():
                if command == "stop":
                    return await fixture["service"].control(fixture["loop_id"], "stop")
                return await LoopAuthorityService(sessions).mutate(fixture["loop_id"], RevokeLoopGrantRequest(command="revoke"))

            controlled, retried = await asyncio.gather(control(), fixture["service"].resolve_wait_request(
                fixture["loop_id"], wait["request_id"], _answer(wait), actor_id="user"), return_exceptions=True)
            assert not isinstance(controlled, BaseException)
            assert not isinstance(retried, BaseException) or isinstance(retried, HTTPException) and retried.status_code == 409
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture["loop_id"])
                row = await session.get(LoopWorkerRequest, identity)
                assert loop.status != "running" and row.status in {"error", "cancelled"}
            assert await runtime.drain(fixture["loop_id"]) == 0 and len(provider.inputs) == 6
    asyncio.run(run())
