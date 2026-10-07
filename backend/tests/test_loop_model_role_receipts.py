"""本文件对外提供 Patrol、Curator、Verifier 与记忆生产模型端口的消费归属验收。

输入为真实冻结 Loop、实际角色运行器和仅替换远端响应的 Provider；输出为逐请求可信 owner、已报告用量和重复回填不重复消费断言。
具体工作流为复用真实 DeepSeek adapter 的请求投影、只替换远端响应，调用生产记忆模型及 PortfolioPatrol，再经 WorkerRuntime 领取并执行两种角色，读取共享预留和账本；不替换 receipt 或模型包装器。
主执行与后代另由 test_loop_governance_accounting 覆盖。示例：pytest backend/tests/test_loop_model_role_receipts.py。
"""

import asyncio
from datetime import UTC, datetime
import json
import os
import uuid

import pytest
from langchain_core.messages import AIMessage
from focus.models.deepseek import DeepSeekChatOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.accounting_query import LoopAccountingQuery
from backend.app.desktop.agent_loop.context_expansion.index_budget_repository import IndexBudgetReservationRepository
from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
from backend.app.desktop.agent_loop.models import LoopBudgetUsage, LoopWorkerRequest
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.patrol import PortfolioPatrol
from backend.app.desktop.agent_loop.round_orchestration import StructuredPatrolDecisionModel
from backend.app.desktop.agent_loop.structured_worker import StructuredWorkerModel
from backend.app.desktop.agent_loop.task_progress.models import LoopProgressWork
from backend.app.desktop.agent_loop.task_progress.runtime import TaskProgressRuntime
from backend.app.desktop.agent_loop.usage import LoopUsageDelta
from backend.app.desktop.agent_loop.workers import LoopWorkerRuntime
from backend.tests.config_helpers import app_config_for
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_round_task_progress import _Checkpointer

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


async def _run_worker(sessions, config, fixture, kind):
    identity = uuid.uuid4().hex
    async with sessions.begin() as session:
        session.add(LoopWorkerRequest(worker_request_id=identity, loop_id=fixture["loop_id"],
            round_id=fixture["round_id"], kind=kind, scope={}, status="pending"))
    runtime = LoopWorkerRuntime(sessions, config)
    try:
        assert await runtime.drain(fixture["loop_id"]) == 1
        await asyncio.wait_for(asyncio.gather(*runtime._tasks.values()), 10)
        async with sessions() as session:
            request = await session.get(LoopWorkerRequest, identity)
            assert request.status == "success", request.result
    finally:
        await runtime.close()
    return identity


def test_production_roles_record_owned_attempts_once(tmp_path, monkeypatch):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fixture = await _seed_loop(sessions, tmp_path, label="role-receipt", started_at=datetime.now(UTC))
        config = app_config_for("role-receipt", None)
        config.models[0].curation_output_method = "prompt_json"
        response = {}
        calls = []

        class Provider(DeepSeekChatOpenAI):
            async def ainvoke(self, messages, config):
                calls.append(messages[0].content)
                config["callbacks"][0].usage_metadata["role-receipt"] = {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}
                return AIMessage(content=json.dumps(response))

        provider = Provider(model="role-receipt", api_key="fixture", base_url="https://fixture.test/v1",
                            max_tokens=config.models[0].curation_max_output_tokens)
        monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", lambda **kwargs: provider)
        monkeypatch.setattr("backend.app.desktop.agent_loop.round_orchestration.create_chat_model", lambda **kwargs: provider)
        try:
            observation = await LoopObservationService(sessions, _Checkpointer()).capture(fixture["loop_id"], fixture["round_id"])
            response.update(source_assessments=[{"source_key": item["source_key"], "disposition": "unknown", "explanation": "保留真实冻结来源"} for item in observation.task_delta["sources"]])
            assert await TaskProgressRuntime(sessions, config, model_factory=lambda name: StructuredWorkerModel(config)).drain(loop_id=fixture["loop_id"]) == 1
            response.clear()
            response.update(rationale="在现有 Context 内推进", mission_references=[{"role": "outcome", "reference_id": "outcome"}],
                actions=[{"action": "continue_context", "context_id": fixture["context_id"], "context_revision_id": fixture["revision_id"], "message": "执行实际检查"}])
            patrol = StructuredPatrolDecisionModel(config)
            intent = await PortfolioPatrol(sessions, patrol).decide(observation, fixture["snapshot"]["holder_id"])
            assert intent.round_id == fixture["round_id"] and patrol.usage_managed
            response.clear()
            response.update(rationale="现有 Context 足够", work_specs=[])
            curator = await _run_worker(sessions, config, fixture, "lane_curator")
            response.clear()
            response.update(criteria=[{"check_id": "tests", "status": "unknown", "explanation": "尚无真实通过证据"}], conclusion="unknown")
            verifier = await _run_worker(sessions, config, fixture, "completion_verifier")
            assert len(calls) == 4
            async with sessions() as session:
                receipts = tuple((await session.scalars(select(LoopIndexBudgetReservation).where(LoopIndexBudgetReservation.loop_id == fixture["loop_id"]))).all())
                assert len(receipts) == 3
                assert {row.actual_usage["owner_kind"] for row in receipts} == {"patrol", "worker"}
                assert {row.actual_usage["owner_id"] for row in receipts if row.actual_usage["owner_kind"] == "worker"} == {curator, verifier}
                assert all(row.actual_usage["round_id"] == fixture["round_id"] for row in receipts)
                work = await session.get(LoopProgressWork, observation.decision_inputs_ref)
                assert work.state == "published" and work.usage[0]["usage_reported"] and work.usage[0]["accounted"]
                usage = await session.get(LoopBudgetUsage, fixture["loop_id"])
                assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (4, 160, 40)
                view = await LoopAccountingQuery().read(session, fixture["loop_id"])
                assert view["consumption"]["actual"] == {"model_calls": 4, "input_tokens": 160, "output_tokens": 40}
            for receipt in receipts:
                repository = IndexBudgetReservationRepository(sessions, fixture["loop_id"], 1, {}, {}, owner=receipt.actual_usage)
                repository._reservation_id = receipt.reservation_id
                await repository.settle(LoopUsageDelta(model_calls=1, input_tokens=40, output_tokens=10))
            async with sessions() as session:
                usage = await session.get(LoopBudgetUsage, fixture["loop_id"])
                assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (4, 160, 40)
        finally:
            provider.client._client.close()
            await _stop(fixture["service"], fixture["loop_id"])
            await engine.dispose()
    asyncio.run(run())
