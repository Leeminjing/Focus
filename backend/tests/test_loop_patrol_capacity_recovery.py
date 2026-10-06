"""本文件对外提供 D6 完整冻结来源、请求容量和准备恢复的隔离验收。

输入为 3 MB Worker 材料、真实 PostgreSQL Loop/claim、脚本化 Provider 和索引失败；输出为原文可重建、来源越权拒绝、超窗零调用及准备不落穿的断言。
具体工作流为先冻结真实来源，再按 hash 分页并捕获模型发送消息；生产准备入口重放/竞争验证唯一派发，失败收口同时核对 Round、Patrol 和等待。
示例：pytest backend/tests/test_loop_patrol_capacity_recovery.py -q；受控 Provider 不替代原生实际模型验收。
"""

import asyncio
from datetime import UTC, datetime
import json
import os
from types import SimpleNamespace
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop import round_orchestration as module
from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.decision_context import DecisionSupplementRepository, PatrolDecisionContext
from backend.app.desktop.agent_loop.authority_control import LoopAuthorityService
from backend.app.desktop.agent_loop.models import AgentLoop, LoopObservation, LoopPatrolAttempt, LoopRound, LoopWorkerRequest
from backend.app.desktop.agent_loop.observation import observation_hash
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.patrol_read_view import PatrolReadView, PatrolWorkerReadRequest
from backend.app.desktop.agent_loop.patrol_request_capacity import PatrolRequestCapacity
from backend.app.desktop.agent_loop.patrol_runtime import CuratorCoordinationStage
from backend.app.desktop.agent_loop.patrol_session_models import LoopCuratorAssignment, LoopPatrolSession
from backend.app.desktop.agent_loop.patrol_session_state import PatrolActivity, PatrolPhase
from backend.app.desktop.agent_loop.patrol_session_repository import PatrolSessionRepository
from backend.app.desktop.agent_loop.ownership import KernelFencingRejected
from backend.app.desktop.agent_loop.round_orchestration import LoopRoundOrchestrator, PatrolCognitiveStep, StructuredPatrolDecisionModel
from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope, RevokeLoopGrantRequest
from backend.app.desktop.agent_loop.task_progress.contracts import canonical_hash
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest
from backend.app.desktop.agent_loop.context_expansion.candidate_paging import ProviderRequestWindowError
from backend.app.desktop.agent_loop.context_expansion.index_model_budget import IndexBudgetExceeded, IndexModelBudget
from backend.tests.config_helpers import app_config_for
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_round_task_progress import _Checkpointer


def _observation():
    return LoopObservationEnvelope(loop_id="loop", round_id="round", loop_revision=1, goal_revision=1,
        authority_revision=1, observed_frontier_hash="a" * 64, mission={"outcome": "deliver"}, grant={},
        portfolio_frontier=(), workspace={"revision": 1}, budget={})


def _config(window=None):
    config = app_config_for("patrol-test", None)
    config.models[0].context_window = window
    config.models[0].curation_output_method = "prompt_json"
    config.models[0].curation_max_output_tokens = 512
    return config


def test_three_megabyte_read_view_keeps_all_sources_and_exact_paging():
    workers = tuple({"request_id": str(index), "round_id": "history", "kind": "lane_curator", "status": "success",
        "result": {"planning_session": "x" * 200000, "exact_reads": ["原文\n保留"], "work_specs": []}} for index in range(16))
    observation = _observation().model_copy(update={"worker_results": workers})
    frozen_hash = observation_hash(observation)
    payload = PatrolReadView.payload(observation)
    assert len(json.dumps(payload).encode()) < 30000
    assert {item["request_id"] for item in payload["worker_source_catalog"]} == {str(index) for index in range(16)}
    assert all(not item["current_round"] for item in payload["worker_source_catalog"])
    for worker in workers:
        cursor, chunks = 0, []
        while cursor is not None:
            page = PatrolReadView.page(worker, PatrolWorkerReadRequest(request_id=worker["request_id"],
                result_hash=canonical_hash(worker["result"]), cursor=cursor, max_bytes=65536))
            chunks.append(page["content"])
            cursor = page["next_cursor"]
        assert json.loads("".join(chunks)) == worker["result"]
    assert observation_hash(observation) == frozen_hash


@pytest.mark.parametrize("method", ["prompt_json", "json_schema", "json_mode"])
def test_oversize_complete_request_has_zero_provider_calls_and_receipts(method):
    class Provider:
        async def ainvoke(self, *args, **kwargs):
            raise AssertionError("超窗不可发送")
        def with_structured_output(self, *args, **kwargs):
            raise AssertionError("超窗不可构造发送路径")
    class Receipts:
        async def reserve(self, *args):
            raise AssertionError("超窗不可虚构已调用预留")

    decision_model = StructuredPatrolDecisionModel(_config(1024))
    decision_model.bind_usage_receipts(Receipts())
    with pytest.raises(ProviderRequestWindowError):
        asyncio.run(decision_model._invoke(Provider(), [HumanMessage(content="x" * 3000)], method, PatrolCognitiveStep))
    assert decision_model.call_count == 0
    assert decision_model.usage.model_calls == 0
    assert decision_model.attempt_metadata == []


def test_capacity_counts_schema_feedback_accumulated_reads_and_output_reserve():
    config = _config().models[0]
    messages = [HumanMessage(content="system/frozen"), HumanMessage(content="validation-feedback"), HumanMessage(content="reads" * 1000)]
    unrestricted = PatrolRequestCapacity(config, {"expansion_resources": {"max_request_input_tokens": None}})
    measured = unrestricted.check(messages, PatrolCognitiveStep)
    config.context_window = measured + 512
    assert PatrolRequestCapacity(config, {}).check(messages, PatrolCognitiveStep) == measured
    with pytest.raises(ProviderRequestWindowError):
        PatrolRequestCapacity(config, {"expansion_resources": {"output_token_reserve": 1024}}).check(messages, PatrolCognitiveStep)
    with pytest.raises(ProviderRequestWindowError):
        PatrolRequestCapacity(config, {}).check([*messages, HumanMessage(content="新增读取")], PatrolCognitiveStep)


def test_index_capacity_diagnostic_preserves_sizes_without_raw_source():
    policy = SimpleNamespace(max_request_input_tokens=None, output_token_reserve=512)
    resources = SimpleNamespace(policy=policy, global_model_calls_remaining=None,
        global_input_tokens_remaining=None, global_output_tokens_remaining=None)
    budget = IndexModelBudget(resources)
    model = SimpleNamespace(context_window_tokens=2048, max_output_tokens=512)
    payload = {"inventory": ["完整来源" * 1000], "segments": ["原文不可截断" * 1000]}
    with pytest.raises(IndexBudgetExceeded) as failure:
        asyncio.run(budget.admit(model, PatrolCognitiveStep, "冻结 system", payload))
    diagnostic = failure.value.request_diagnostic
    assert diagnostic["schema"] == "PatrolCognitiveStep"
    assert len(diagnostic["request_hash"]) == 64
    assert diagnostic["input_bound"] > diagnostic["input_limit"] == 1536
    assert diagnostic["output_reserve"] == 512
    assert set(diagnostic["payload_field_bytes"]) == {"inventory", "segments"}
    assert "原文不可截断" not in json.dumps(diagnostic, ensure_ascii=False)
    assert "Tool Exchange" not in str(failure.value)


def test_actual_patrol_transport_uses_catalog_without_inline_inventory(monkeypatch):
    worker = {"request_id": "history-worker", "round_id": "history", "kind": "lane_curator", "status": "success",
              "result": {"planning_session": "physical" * 450000, "work_specs": []}}
    observation = _observation().model_copy(update={"worker_results": (worker,),
        "grant": {"holder_id": "holder", "grant_id": "grant"},
        "user_intents": ({"intent_id": "intent", "intent_kind": "patrol_opinion", "content": "原文" * 4000},)})
    response = {"rationale": "当前 Context 足够", "mission_references": [{"role": "outcome", "reference_id": "outcome"}],
        "actions": [{"action": "continue_context", "context_id": "context", "context_revision_id": "revision", "message": "继续"}]}
    class Provider:
        received = []
        async def ainvoke(self, messages, **kwargs):
            self.received.append(messages)
            return AIMessage(content=json.dumps(response))
    provider = Provider()
    monkeypatch.setattr(module, "create_chat_model", lambda **kwargs: provider)
    config = _config(128000)
    intent = asyncio.run(StructuredPatrolDecisionModel(config)(observation))
    assert intent.actions[0].action == "continue_context"
    payload = json.loads(str(provider.received[0][1].content).removeprefix("<loop_observation>").removesuffix("</loop_observation>"))
    assert "physical" not in str(provider.received)
    assert payload["worker_source_catalog"][0]["result_hash"] == canonical_hash(worker["result"])
    assert payload["user_intents"][0]["content"] == "原文" * 4000


def test_invalid_page_cursor_or_hash_is_not_silently_clipped():
    worker = {"request_id": "worker", "result": {"text": "原文" * 1000}}
    request = PatrolWorkerReadRequest(request_id="worker", result_hash=canonical_hash(worker["result"]))
    with pytest.raises(ValueError, match="UTF-8"):
        PatrolReadView.page(worker, request.model_copy(update={"cursor": 10}))
    with pytest.raises(ValueError, match="超出"):
        PatrolReadView.page(worker, request.model_copy(update={"cursor": 999999}))
    with pytest.raises(ValueError, match="hash"):
        PatrolReadView.page(worker, request.model_copy(update={"result_hash": "b" * 64}))


@pytest.mark.usefixtures("isolated_postgres_database")
def test_worker_pages_validate_frozen_domain_hash_and_current_authority(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="pages", started_at=datetime.now(UTC))
        service = LoopObservationService(sessions, _Checkpointer())
        request_id = uuid.uuid4().hex
        result = {"planning_session": "实际原文" * 30000}
        try:
            async with sessions.begin() as session:
                session.add(LoopWorkerRequest(worker_request_id=request_id, loop_id=fixture["loop_id"],
                    round_id=fixture["round_id"], kind="lane_curator", status="success", scope={}, result=result))
                for index in range(19):
                    session.add(LoopWorkerRequest(worker_request_id=uuid.uuid4().hex, loop_id=fixture["loop_id"],
                        round_id=fixture["round_id"], kind="lane_curator", status="success", scope={}, result={"source": index}))
            observation = await service.capture(fixture["loop_id"], fixture["round_id"])
            assert len(observation.worker_results) == 20
            assert len(await service.worker_sources(observation)) == 20
            frozen_hash = observation_hash(observation)
            current_id = uuid.uuid4().hex
            current = {"request_id": current_id, "round_id": fixture["round_id"], "kind": "lane_curator",
                       "status": "proposed", "result": {"planning_session": "本轮补充原文" * 1000}}
            async with sessions.begin() as session:
                session.add(LoopWorkerRequest(worker_request_id=current_id, loop_id=fixture["loop_id"],
                    round_id=fixture["round_id"], kind="lane_curator", status="success", scope={}, result=current["result"]))
                observation_id = await session.scalar(select(LoopObservation.observation_id).where(LoopObservation.round_id == fixture["round_id"]))
                await DecisionSupplementRepository().put(session, observation_id, "curator_results", {"results": [current]})
            composed = PatrolDecisionContext(observation, curator_results=(current,)).model_observation()
            catalog = await service.worker_sources(composed)
            view = PatrolReadView.payload(composed, catalog)
            assert len(view["worker_source_catalog"]) == 21
            assert {item["request_id"] for item in view["worker_results"]} == {current_id}
            current_page = (await service.selective_read(composed, (PatrolWorkerReadRequest(
                request_id=current_id, result_hash=canonical_hash(current["result"]), max_bytes=65536),)))[0]
            assert json.loads(current_page["content"]) == current["result"]
            request = PatrolWorkerReadRequest(request_id=request_id, result_hash=canonical_hash(result))
            page = (await service.selective_read(observation, (request,)))[0]
            assert page["next_cursor"] is not None and page["round_id"] == fixture["round_id"]
            with pytest.raises(ValueError, match="领域"):
                await service.selective_read(observation, (request.model_copy(update={"request_id": "outside"}),))
            with pytest.raises(ValueError, match="改变"):
                await service.selective_read(observation, (request.model_copy(update={"result_hash": "b" * 64}),))
            async with sessions.begin() as session:
                worker = await session.get(LoopWorkerRequest, request_id)
                worker.result = {"changed": True}
            with pytest.raises(ValueError, match="改变"):
                await service.selective_read(observation, (request,))
            await LoopAuthorityService(sessions).mutate(fixture["loop_id"], RevokeLoopGrantRequest(command="revoke"))
            with pytest.raises(ValueError, match="授权"):
                await service.selective_read(observation, (request,))
            async with sessions() as session:
                frozen = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == fixture["round_id"]))
                assert frozen.envelope_hash == frozen_hash
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_old_preparation_owner_cannot_dispatch_or_fail_replacement(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="fence", started_at=datetime.now(UTC))
        coordinator = LoopCoordinator(sessions)
        orchestrator = LoopRoundOrchestrator(sessions, _config(), None, _Checkpointer())
        orchestrator._curators = CuratorCoordinationStage(sessions, index_service=_Index())
        try:
            old = await coordinator.claim_for_loop(fixture["loop_id"], "old-owner")
            observation = await orchestrator._observations.capture(old.loop_id, old.round_id)
            patrol = await orchestrator._patrol_sessions.begin(old)
            await orchestrator._patrol_sessions.transition(patrol.session_id, PatrolPhase.OBSERVING, PatrolActivity(summary="冻结就绪"))
            await orchestrator._patrol_sessions.transition(patrol.session_id, PatrolPhase.DISPATCHING_CURATORS, PatrolActivity(summary="派发准备"))
            await coordinator.release(old)
            replacement = await coordinator.claim_for_loop(fixture["loop_id"], "new-owner")
            await orchestrator._fail(old, RuntimeError("新 owner 尚未进入 Patrol 的迟到失败"))
            async with sessions() as session:
                assert (await session.get(LoopRound, old.round_id)).status == "observed"
                assert (await session.get(LoopPatrolSession, patrol.session_id)).status == "active"
            await orchestrator._patrol_sessions.begin(replacement)
            with pytest.raises(KernelFencingRejected):
                await orchestrator._curators.dispatch(patrol.session_id, observation,
                    orchestrator._curators.scopes(observation), fencing_token=int(old.fencing_token))
            await orchestrator._fail(old, RuntimeError("迟到失败"))
            async with sessions() as session:
                assert (await session.get(LoopRound, old.round_id)).status == "observed"
                assert (await session.get(LoopPatrolSession, patrol.session_id)).status == "active"
                assert await session.scalar(select(func.count()).select_from(LoopCuratorAssignment).where(LoopCuratorAssignment.session_id == patrol.session_id)) == 0
            assert await orchestrator._prepare_observation(replacement, patrol.session_id, "observed") is None
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
def test_round_and_patrol_failure_roll_back_together(tmp_path, monkeypatch):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="atomic", started_at=datetime.now(UTC))
        coordinator = LoopCoordinator(sessions)
        orchestrator = LoopRoundOrchestrator(sessions, _config(), None, _Checkpointer())
        try:
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "atomic-owner")
            await orchestrator._observations.capture(claim.loop_id, claim.round_id)
            patrol = await orchestrator._patrol_sessions.begin(claim)
            async def refuse(*args, **kwargs):
                raise RuntimeError("transition-write-failed")
            with monkeypatch.context() as patch:
                patch.setattr(PatrolSessionRepository, "transition", refuse)
                with pytest.raises(RuntimeError, match="transition-write-failed"):
                    await orchestrator._fail(claim, ValueError("provider_request_window"))
            async with sessions() as session:
                assert (await session.get(LoopRound, claim.round_id)).status == "observed"
                assert (await session.get(LoopPatrolSession, patrol.session_id)).status == "active"
                assert await session.scalar(select(func.count()).select_from(LoopWaitRequest).where(LoopWaitRequest.loop_id == claim.loop_id)) == 0
            await orchestrator._fail(claim, ValueError("provider_request_window"))
            async with sessions() as session:
                assert (await session.get(LoopRound, claim.round_id)).status == "error"
                assert (await session.get(LoopPatrolSession, patrol.session_id)).status == "failed"
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())


class _Index:
    def __init__(self, fail=False):
        self.fail = fail
    async def build(self, observation):
        return SimpleNamespace(catalog=None if self.fail else SimpleNamespace(model_dump=lambda **kwargs: {"catalog_id": "a" * 64}),
            indexes=(), stage_record=None, blocker_code="provider_request_window", blocker_summary="Tool Exchange remains indivisible")


@pytest.mark.usefixtures("isolated_postgres_database")
@pytest.mark.parametrize("fail", [False, True])
def test_interrupted_dispatch_resumes_durable_preparation_or_atomically_fails(tmp_path, fail):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="prepare", started_at=datetime.now(UTC))
        coordinator = LoopCoordinator(sessions)
        orchestrator = LoopRoundOrchestrator(sessions, _config(), None, _Checkpointer())
        orchestrator._curators = CuratorCoordinationStage(sessions, index_service=_Index(fail))
        try:
            claim = await coordinator.claim_for_loop(fixture["loop_id"], "prepare-owner")
            observation = await orchestrator._observations.capture(claim.loop_id, claim.round_id)
            patrol = await orchestrator._patrol_sessions.begin(claim)
            await orchestrator._patrol_sessions.transition(patrol.session_id, PatrolPhase.OBSERVING, PatrolActivity(summary="冻结已完成"))
            await orchestrator._patrol_sessions.transition(patrol.session_id, PatrolPhase.DISPATCHING_CURATORS, PatrolActivity(summary="准备期间中断"))
            if fail:
                assert await orchestrator.process(claim) is None
            else:
                assert await orchestrator._prepare_observation(claim, patrol.session_id, "observed") is None
                scopes = orchestrator._curators.scopes(observation)
                assert await asyncio.gather(*(orchestrator._curators.dispatch(patrol.session_id, observation, scopes,
                    fencing_token=int(claim.fencing_token)) for _ in range(2))) == [1, 1]
            async with sessions() as session:
                round_row = await session.get(LoopRound, claim.round_id)
                patrol_row = await session.get(LoopPatrolSession, patrol.session_id)
                assert await session.scalar(select(func.count()).select_from(LoopPatrolAttempt).where(LoopPatrolAttempt.round_id == claim.round_id)) == 0
                assignments = await session.scalar(select(func.count()).select_from(LoopCuratorAssignment).where(LoopCuratorAssignment.session_id == patrol.session_id))
                if fail:
                    assert assignments == 0 and round_row.status == "error" and patrol_row.status == "failed"
                    assert patrol_row.terminal_outcome["failed_phase"] == "dispatching_curators"
                    assert patrol_row.terminal_outcome["reason_code"] == "provider_request_window"
                    assert (await session.get(AgentLoop, claim.loop_id)).status == "waiting_user"
                    assert await session.scalar(select(func.count()).select_from(LoopWaitRequest).where(LoopWaitRequest.loop_id == claim.loop_id)) == 1
                else:
                    assert assignments == 1 and patrol_row.current_phase == "collecting_curators"
            if fail:
                await orchestrator._fail(claim, ValueError("重复失败"))
                assert await orchestrator.process(claim) is None
        finally:
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
