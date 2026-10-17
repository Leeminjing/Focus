"""本文件对外提供规划 Worker 的冻结观察隔离与重试恢复数据库回归。

输入为隔离 PostgreSQL、真实 Loop/Observation、检索索引及受控模型响应；输出为同轮新观察
使用新规划、相同输入复用检查点与用量、错误绑定和过期写入被拒绝的断言。具体工作流为
启动 Loop、持久冻结观察、实际领取 Worker、装配输入并经真实规划器与仓储保存 Session。
仅替换模型响应，不替换身份计算、授权校验或 Session 仓储。示例：pytest 本文件 -q。
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.context_expansion.artifact_repository import SemanticDerivationArtifactRepository
from backend.app.desktop.agent_loop.context_expansion.models import LoopPlanningRetrievalSession
from backend.app.desktop.agent_loop.context_expansion.retrieval_planner import LaneAdviceProposal, PlannerQueryProposal, PlannerReadProposal
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import RevisionSemanticIndexer
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import PortfolioIndexCatalog
from backend.app.desktop.agent_loop.expansion_resource_policy import resolve_expansion_resources
from backend.app.desktop.agent_loop.models import LoopWorkerRequest
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.worker_attempts import WorkerAttemptRejected
from backend.app.desktop.agent_loop.workers import LoopWorkerRuntime
from backend.tests.config_helpers import app_config_for
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_context_expansion_kernel import _source_ref
from backend.tests.test_patrol_session import _persist_observation
from backend.tests.test_round_task_progress import _Checkpointer
from backend.app.desktop.evolution_strategies import strategy_catalog
from focus.history import content_hash

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _Model:
    def __init__(self):
        self.inputs = []
        self.before_call = None
        self.block = False
        self.read_strategy = False

    async def invoke(self, schema, system, payload):
        if self.before_call:
            await self.before_call(schema)
        self.inputs.append((schema, deepcopy(payload)))
        if schema is PlannerQueryProposal:
            return schema(rationale="Find requirement", queries=({"text": "durable lock", "kinds": ("semantic_unit",), "limit": 2},))
        if schema is PlannerReadProposal:
            return schema(rationale="Read requirement", candidate_ids=() if self.block else (payload["candidates"][0]["candidate_id"],),
                strategy_reads=(payload["derivation"]["evolution_strategies"][0]["read"],) if self.read_strategy else ())
        assert schema is LaneAdviceProposal
        return schema(rationale=f"fresh-plan-{len(self.inputs)}", work_specs=())


@asynccontextmanager
async def _case(tmp_path):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed_loop(sessions, tmp_path, label="binding", started_at=datetime.now(UTC))
    runtime = LoopWorkerRuntime(sessions, app_config_for("binding", None))
    try:
        base = await LoopObservationService(sessions, _Checkpointer()).capture(seeded["loop_id"], seeded["round_id"])
        strategy = {"memory_id": "strategy", "title": "方法", "workspace_id": None,
                    "strategy": {"applicability": "适用", "evolution": "diagnose", "inheritance": "约束"}}
        base = base.model_copy(update={
            "evolution_strategies": ({**strategy, "content_hash": content_hash(strategy)},),
            "user_intents": ({"scope": "portfolio", "content": "Please reconsider", "file_refs": [{"path": "plan.md", "sha256": "a" * 64}]},),
        })
        observation = await _persist_observation(sessions, base, generation=base.generation + 1)
        index = RevisionSemanticIndexer().index(source=_source_ref(seeded), source_content_hash="a" * 64,
            context_role="requirements", active_objective="Verify lock",
            raw_messages=({"id": "requirement", "role": "human", "content": "The durable lock requirement must be verified."},))
        async with sessions.begin() as session:
            await SemanticDerivationArtifactRepository().put_index(session, index)
        catalog = PortfolioIndexCatalog.create(frontier_hash=observation.observed_frontier_hash, indexes=(index,))
        scope = {"assignments": [{"context_id": seeded["context_id"], "role": "requirements"}], "derivation_input": {
            "mission": observation.mission, "semantic_index_ids": [index.index_id],
            "portfolio_index_catalog": catalog.model_dump(mode="json"),
            "frozen_expansion_resources": resolve_expansion_resources({}, observation.authority_revision, {}).model_dump(mode="json"),
        }}
        yield SimpleNamespace(sessions=sessions, runtime=runtime, seeded=seeded, observation=observation,
            index=index, scope=scope, model=_Model())
    finally:
        await runtime.close()
        await _stop(seeded["service"], seeded["loop_id"])
        await engine.dispose()


async def _request(case, observation):
    scope = deepcopy(case.scope)
    scope["derivation_input"]["decision_inputs_ref"] = observation.observation_id
    async with case.sessions.begin() as session:
        session.add(LoopWorkerRequest(worker_request_id=uuid.uuid4().hex, loop_id=observation.loop_id,
            round_id=observation.round_id, observation_id=observation.observation_id,
            observation_generation=observation.generation, kind="lane_curator", scope=scope))
    return (await case.runtime._claim_many(1, observation.loop_id))[0]


async def _plan(case, request):
    payload, loop, _ = await case.runtime._evidence(request)
    return await case.runtime._retrieval_backed_advice(request, payload, case.model,
        (case.index.index_id,), loop.authority_revision)


async def _saved(case):
    async with case.sessions() as session:
        rows = await session.scalars(select(LoopPlanningRetrievalSession).where(
            LoopPlanningRetrievalSession.loop_id == case.observation.loop_id))
        return {row.session_id: deepcopy(row.payload) for row in rows}


def test_worker_strategy_reader_resolves_the_bound_observation(tmp_path):
    async def run():
        async with _case(tmp_path) as case:
            case.model.read_strategy = True
            result = await _plan(case, await _request(case, case.observation))
            assert result["planning_session"]["state"] == "planned"
            pages = result["planning_session"]["strategy_read_pages"]
            assert len(pages) == 1 and pages[0]["content"] == "diagnose"
            assert pages[0]["content_hash"] == case.observation.evolution_strategies[0]["content_hash"]
    asyncio.run(run())


@pytest.mark.parametrize("change", ["strategy", "feedback", "file", "generation"])
def test_new_observation_replans_through_persisted_worker_session(tmp_path, change):
    async def run():
        async with _case(tmp_path) as case:
            first = await _plan(case, await _request(case, case.observation))
            assert first["planning_session"]["state"] == "planned"
            old = await _saved(case)
            count = len(case.model.inputs)
            updated = case.observation.model_copy(deep=True)
            if change == "strategy":
                updated.evolution_strategies[0]["strategy"]["evolution"] = "reconstruct"
                updated.evolution_strategies[0]["content_hash"] = content_hash({key: value for key, value in updated.evolution_strategies[0].items() if key != "content_hash"})
            elif change == "feedback":
                updated.user_intents[0]["content"] = "Use an offline design"
            elif change == "file":
                updated.user_intents[0]["file_refs"][0]["sha256"] = "b" * 64
            updated = await _persist_observation(case.sessions, updated, generation=updated.generation + 1)
            second = await _plan(case, await _request(case, updated))
            assert second["planning_session"]["session_id"] != first["planning_session"]["session_id"]
            assert second["planning_session"]["state"] == "planned"
            assert len(case.model.inputs) > count
            for _, payload in case.model.inputs[count:]:
                assert payload["derivation"]["evolution_strategies"] == strategy_catalog(updated.evolution_strategies)
                assert payload["derivation"]["user_intents"] == updated.user_intents
                assert payload["derivation"]["observation_id"] == updated.observation_id
            saved = await _saved(case)
            assert len(saved) == 2
            assert all(saved[key] == value for key, value in old.items())
    asyncio.run(run())


@pytest.mark.parametrize("interrupted", [False, True])
def test_same_observation_retry_reuses_result_or_checkpoint_and_usage(tmp_path, interrupted):
    async def run():
        async with _case(tmp_path) as case:
            request = await _request(case, case.observation)

            async def interrupt_after_queries(schema):
                if schema is PlannerReadProposal:
                    raise asyncio.CancelledError()

            if interrupted:
                case.model.before_call = interrupt_after_queries
                with pytest.raises(asyncio.CancelledError):
                    await _plan(case, request)
            else:
                await _plan(case, request)
            before = await _saved(case)
            identity, checkpoint = next(iter(before.items()))
            assert checkpoint["usage"]["model_calls"] == len(case.model.inputs)
            assert checkpoint["state"] == ("queried" if interrupted else "planned")
            await case.runtime._fail(request, RuntimeError("response lost"))
            async with case.sessions.begin() as session:
                row = await session.get(LoopWorkerRequest, request.worker_request_id)
                row.scope = {**row.scope, "frozen_worker_input": {"mission": {"outcome": "obsolete cached input"}}}
            case.runtime = LoopWorkerRuntime(case.sessions, app_config_for("binding", None))
            retry = (await case.runtime._claim_many(1, request.loop_id))[0]
            assert retry.retry_identity != request.retry_identity
            case.model.before_call = None
            result = await _plan(case, retry)
            assert result["planning_session"]["session_id"] == identity
            assert len(await _saved(case)) == 1
            assert result["planning_session"]["usage"]["model_calls"] == len(case.model.inputs) == 3
            assert sum(schema is PlannerQueryProposal for schema, _ in case.model.inputs) == 1
            if not interrupted:
                assert await _saved(case) == before
            else:
                charges = result["planning_session"]["ledger"]["charges"]
                assert charges[:len(checkpoint["ledger"]["charges"])] == checkpoint["ledger"]["charges"]
    asyncio.run(run())


def test_new_observation_does_not_inherit_blocked_session(tmp_path):
    async def run():
        async with _case(tmp_path) as case:
            case.model.block = True
            first = await _plan(case, await _request(case, case.observation))
            assert first["planning_session"]["state"] == "blocked"
            old = await _saved(case)
            updated = await _persist_observation(case.sessions, case.observation, generation=case.observation.generation + 1)
            case.model.block = False
            second = await _plan(case, await _request(case, updated))
            assert second["planning_session"]["state"] == "planned"
            saved = await _saved(case)
            assert len(saved) == 2
            assert all(saved[key] == value for key, value in old.items())
    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["missing", "missing_record", "generation", "reference", "loop", "round"])
def test_invalid_binding_never_falls_back_to_cached_or_current_input(tmp_path, invalid):
    async def run():
        async with _case(tmp_path) as case:
            request = await _request(case, case.observation)
            request.scope = {**request.scope, "frozen_worker_input": {"mission": {"outcome": "stale input"}}}
            if invalid == "missing":
                request.observation_id = None
            elif invalid == "missing_record":
                request.observation_id = uuid.uuid4().hex
            elif invalid == "generation":
                request.observation_generation += 1
            elif invalid == "reference":
                request.scope["derivation_input"]["decision_inputs_ref"] = uuid.uuid4().hex
            else:
                async with _case(tmp_path) as other:
                    request.loop_id = other.observation.loop_id if invalid == "loop" else request.loop_id
                    request.round_id = other.observation.round_id
            with pytest.raises(ValueError, match="Observation"):
                await _plan(case, request)
            assert case.model.inputs == []
            assert await _saved(case) == {}
    asyncio.run(run())


def test_new_observation_rejects_old_worker_checkpoint(tmp_path):
    async def run():
        async with _case(tmp_path) as case:
            request = await _request(case, case.observation)
            checkpoint = {}

            async def replace_observation(schema):
                if schema is PlannerReadProposal:
                    checkpoint.update(await _saved(case))
                    await _persist_observation(case.sessions, case.observation, generation=case.observation.generation + 1)

            case.model.before_call = replace_observation
            with pytest.raises(WorkerAttemptRejected):
                await _plan(case, request)
            assert await _saved(case) == checkpoint
            assert all(schema is not LaneAdviceProposal for schema, _ in case.model.inputs)
    asyncio.run(run())


def test_old_identity_cache_is_retained_but_never_used_for_new_planning(tmp_path):
    from backend.app.desktop.agent_loop.context_expansion.contracts import stable_expansion_hash
    from backend.app.desktop.agent_loop.context_expansion.retrieval_planner import RetrievalBackedCognitiveAdvisor
    from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import PlanningRetrievalSession, PlanningRetrievalSessionController, RetrievalBudget
    from backend.app.desktop.agent_loop.expansion_resource_policy import FrozenExpansionResources

    async def run():
        async with _case(tmp_path) as case:
            request = await _request(case, case.observation)
            derivation = request.scope["derivation_input"]
            frozen = FrozenExpansionResources.model_validate(derivation["frozen_expansion_resources"])
            policy = frozen.policy
            legacy = PlanningRetrievalSession.create(observation_hash=stable_expansion_hash(
                "retrieval-planning-observation", request.loop_id, request.round_id,
                case.observation.observed_frontier_hash, derivation["mission"], tuple(request.scope["assignments"])),
                frontier_hash=case.observation.observed_frontier_hash,
                catalog=PortfolioIndexCatalog.model_validate(derivation["portfolio_index_catalog"]),
                planner_version=RetrievalBackedCognitiveAdvisor.VERSION, retrieval_version="authorized-lexical-retriever-v1",
                budget=RetrievalBudget(max_queries=policy.max_queries, max_candidates=policy.max_unique_candidates,
                    max_exact_reads=policy.max_exact_reads, max_model_calls=policy.max_planner_model_calls,
                    max_tokens=policy.max_planner_tokens), frozen_resources=frozen)
            legacy = PlanningRetrievalSessionController().block(legacy, "legacy-blocked")
            async with case.sessions.begin() as session:
                await SemanticDerivationArtifactRepository().save_session(session, legacy,
                    loop_id=request.loop_id, round_id=request.round_id)
            result = await _plan(case, request)
            assert result["planning_session"]["state"] == "planned"
            saved = await _saved(case)
            assert len(saved) == 2 and saved[legacy.session_id] == legacy.model_dump(mode="json")
    asyncio.run(run())
