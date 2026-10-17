"""冻结策略目录与按需正文：真实持久快照、权限、分页及模型请求回归。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
import os

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select

from focus.context.requests import frozen_request_messages
from focus.history import content_hash
from backend.app.desktop.evolution_strategies import EvolutionStrategy, strategy_catalog, strategy_catalog_input, freeze_strategies
from backend.app.desktop.memory import MemoryService, MemoryUpdate
from backend.app.desktop.models import Memory
from backend.app.desktop.strategy_packages import StrategyMetadata, StrategyPackage
from backend.app.desktop.strategy_snapshots import FrozenStrategySnapshot, PatrolStrategyReadRequest, read_strategy_page
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.observation import observation_hash
from backend.app.desktop.agent_loop.patrol_read_view import PatrolReadView
from backend.app.desktop.agent_loop.patrol_request_capacity import PatrolRequestCapacity
from backend.app.desktop.agent_loop.round_orchestration import (
    PATROL_SYSTEM_CONTRACT, PatrolCognitiveStep, StructuredPatrolDecisionModel,
)
from backend.tests.config_helpers import app_config_for
from test_agent_loop_round_liveness import _seed_loop, _stop
from test_round_task_progress import _Checkpointer


def config():
    app = app_config_for("lazy-test", None)
    app.models[0].context_window = 131072
    app.models[0].curation_max_output_tokens = 4096
    app.models[0].curation_output_method = "prompt_json"
    return app


def package(name):
    return StrategyPackage(metadata=StrategyMetadata(id=f"lazy/{name}", name=name, version="1.0.0"),
        strategy=EvolutionStrategy(applicability="在需求存在不确定性时使用", evolution="冻结方法" * 3000, inheritance="冻结继承" * 3000))


@asynccontextmanager
async def live(tmp_path):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    seed = await _seed_loop(sessions, tmp_path, label="lazy", started_at=datetime.now(UTC))
    memories = MemoryService(sessions, config(), None)
    observations = LoopObservationService(sessions, _Checkpointer())
    try:
        yield sessions, seed, memories, observations
    finally:
        await _stop(seed["service"], seed["loop_id"])
        async with sessions.begin() as session:
            rows = (await session.scalars(select(Memory).where(Memory.workspace_id == seed["snapshot"]["workspace_id"]))).all()
            for row in rows:
                await session.delete(row)
        await engine.dispose()


def test_catalog_avoids_full_bodies_and_read_survives_edit_disable_delete(isolated_postgres_database, tmp_path):
    async def run():
        async with live(tmp_path) as (sessions, seed, memories, observations):
            scope = seed["snapshot"]["workspace_id"]
            installed = [(await memories.install_strategy(package(str(index)).markdown(), scope))["memory"] for index in range(5)]
            observation = await observations.capture(seed["loop_id"], seed["round_id"])
            frozen_hash = observation_hash(observation)
            catalog = observation.evolution_strategies
            assert len([entry for entry in catalog if entry["title"].isdigit()]) == 5
            assert "冻结方法" not in json.dumps(observation.model_dump(), ensure_ascii=False)
            assert "冻结继承" not in json.dumps(PatrolReadView.payload(observation), ensure_ascii=False)
            entry = next(item for item in catalog if item["memory_id"] == installed[0]["memory_id"])
            original = package("0").strategy
            await memories.update(entry["memory_id"], MemoryUpdate(strategy=original.model_copy(update={"evolution": "后来编辑"})))
            async with sessions.begin() as session:
                later = await freeze_strategies(session, scope)
            assert next(item for item in later if item["memory_id"] == entry["memory_id"])["content_hash"] != entry["content_hash"]
            await memories.update(entry["memory_id"], MemoryUpdate(enabled=False))
            await memories.delete(entry["memory_id"])
            # A fresh reader obtains the immutable version without the installation.
            reader = LoopObservationService(sessions, None)
            chunks, cursor = [], 0
            while True:
                page = (await reader.selective_read(observation, (PatrolStrategyReadRequest(
                    memory_id=entry["memory_id"], content_hash=entry["content_hash"], cursor=cursor, max_bytes=8192),)))[0]
                chunks.append(page["content"])
                assert len(page["content"].encode("utf-8")) <= 8192
                if page["next_cursor"] is None:
                    break
                cursor = page["next_cursor"]
            assert "".join(chunks) == original.evolution
            assert observation_hash(observation) == frozen_hash
            for changes in ({"cursor": 1}, {"cursor": 1000000}, {"content_hash": "f" * 64}, {"memory_id": "outside"}):
                request = PatrolStrategyReadRequest(**{**entry["read"], **changes})
                with pytest.raises(ValueError):
                    await reader.selective_read(observation, (request,))
    asyncio.run(run())


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_snapshot_never_falls_back_to_current_memory(isolated_postgres_database, tmp_path, corrupt):
    async def run():
        async with live(tmp_path) as (sessions, seed, memories, observations):
            installed = (await memories.install_strategy(package("integrity").markdown(), seed["snapshot"]["workspace_id"]))["memory"]
            observation = await observations.capture(seed["loop_id"], seed["round_id"])
            entry = next(item for item in observation.evolution_strategies if item["memory_id"] == installed["memory_id"])
            async with sessions.begin() as session:
                row = await session.get(FrozenStrategySnapshot, entry["content_hash"])
                if corrupt:
                    row.payload = {**row.payload, "strategy": {**row.payload["strategy"], "evolution": "替换正文"}}
                else:
                    await session.delete(row)
            with pytest.raises(ValueError, match="hash|缺失"):
                await observations.selective_read(observation, (PatrolStrategyReadRequest(**entry["read"]),))
    asyncio.run(run())


def test_read_rejects_forged_observation_and_revoked_authority(isolated_postgres_database, tmp_path):
    from backend.app.desktop.agent_loop.authority_control import LoopAuthorityService
    from backend.app.desktop.agent_loop.schemas import RevokeLoopGrantRequest

    async def run():
        async with live(tmp_path) as (sessions, seed, memories, observations):
            observation = await observations.capture(seed["loop_id"], seed["round_id"])
            entry = observation.evolution_strategies[0]
            request = PatrolStrategyReadRequest(**entry["read"])
            forged = observation.model_copy(update={"evolution_strategies": ({**entry, "title": "伪造"},)})
            with pytest.raises(ValueError, match="hash"):
                await observations.selective_read(forged, (request,))
            await LoopAuthorityService(sessions).mutate(seed["loop_id"], RevokeLoopGrantRequest(command="revoke"))
            with pytest.raises(ValueError, match="授权"):
                await observations.selective_read(observation, (request,))
    asyncio.run(run())


@pytest.mark.parametrize("limited", [False, True])
def test_patrol_transport_only_includes_requested_pages_and_checks_next_request(isolated_postgres_database, tmp_path, limited):
    from backend.app.desktop.agent_loop.context_expansion.candidate_paging import ProviderRequestWindowError

    async def run():
        async with live(tmp_path) as (sessions, seed, memories, observations):
            installed = (await memories.install_strategy(package("transport").markdown(), seed["snapshot"]["workspace_id"]))["memory"]
            observation = await observations.capture(seed["loop_id"], seed["round_id"])
            entry = next(item for item in observation.evolution_strategies if item["memory_id"] == installed["memory_id"])
            app = config()
            messages = frozen_request_messages(PATROL_SYSTEM_CONTRACT, json.dumps(PatrolReadView.payload(observation), ensure_ascii=False), scope="round")
            received = []
            class Provider:
                async def ainvoke(self, messages, **kwargs):
                    received.append(messages)
                    if len(received) == 1:
                        return AIMessage(content=json.dumps({"reads": [{**entry["read"], "max_bytes": 256}]}))
                    return AIMessage(content=json.dumps({"rationale": "依据已读取的方法继续", "mission_references": [{"role": "outcome", "reference_id": "outcome"}],
                        "strategy_adoptions": [{"memory_id": entry["memory_id"], "content_hash": entry["content_hash"], "reason": "先验证", "action_indexes": [0]}],
                        "actions": [{"action": "continue_context", "context_id": seed["context_id"], "context_revision_id": seed["revision_id"], "message": "继续验证"}]}))
            if limited:
                schema_message = type(messages[1])(content="只返回符合此 JSON Schema 的 JSON：" + json.dumps(PatrolCognitiveStep.model_json_schema(), ensure_ascii=False, separators=(",", ":")))
                bound = PatrolRequestCapacity(app.models[0], {}).estimate([*messages, schema_message], PatrolCognitiveStep)
                app.models[0].context_window = bound + app.models[0].curation_max_output_tokens
            model = StructuredPatrolDecisionModel(app, reader=observations.selective_read)
            model._remaining_calls = 3
            if limited:
                with pytest.raises(ProviderRequestWindowError):
                    await model._decide(Provider(), messages, "prompt_json", observation)
                assert len(received) == model.call_count == 1
            else:
                result = await model._decide(Provider(), messages, "prompt_json", observation)
                assert result.strategy_adoptions[0].content_hash == entry["content_hash"]
                assert len(received) == 2
                assert "冻结方法" not in str(received[0]) and "冻结方法" in str(received[1])
                assert "冻结继承" not in str(received)
                assert package("transport").strategy.evolution not in str(received[1])
    asyncio.run(run())


def test_legacy_full_snapshot_projects_to_catalog_and_can_be_read_without_store():
    body = {"memory_id": "legacy", "title": "旧策略", "workspace_id": None,
            "strategy": {"applicability": "适用", "evolution": "原有方法", "inheritance": "原有继承"}}
    snapshot = {**body, "content_hash": content_hash(body)}
    catalog = strategy_catalog((snapshot,))
    assert "strategy" not in catalog[0]
    page = asyncio.run(read_strategy_page(None, (snapshot,), PatrolStrategyReadRequest(**catalog[0]["read"])))
    assert page["content"] == "原有方法"
    legacy_input = {"scope": {"derivation_input": {"evolution_strategies": (snapshot,)}}, "user_text": "原文"}
    projected = strategy_catalog_input(legacy_input)
    assert projected["scope"]["derivation_input"]["evolution_strategies"] == catalog
    assert projected["user_text"] == "原文" and "strategy" in legacy_input["scope"]["derivation_input"]["evolution_strategies"][0]


@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("paged", [False, True])
@pytest.mark.parametrize("limited", [False, True])
def test_planner_reads_selected_strategy_and_persists_pages_for_resume(foreign, paged, limited):
    from test_complete_semantic_context_derivation import _long_index
    from backend.app.desktop.agent_loop.context_expansion.retrieval_planner import (
        RetrievalBackedCognitiveAdvisor, PlannerQueryProposal, PlannerReadProposal, PlannerStrategyReadProposal, LaneAdviceProposal,
    )
    from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
        PlanningRetrievalSession, PortfolioIndexCatalog, AuthorizedSemanticRetriever, RetrievalBudget,
    )
    body = {"memory_id": "frozen", "title": "方法", "workspace_id": None,
        "strategy": {"applicability": "适用", "evolution": "按冻结方法执行" * (50 if paged else 1), "inheritance": "未请求的继承"}}
    snapshot = {**body, "content_hash": content_hash(body)}
    catalog = strategy_catalog((snapshot,))
    calls, reads, checkpoints = [], [], []
    model_config = config().models[0]
    from backend.app.desktop.agent_loop.structured_worker import StructuredWorkerModel
    class Model:
        request_model_config = model_config
        request_messages = staticmethod(StructuredWorkerModel.request_messages)
        async def invoke(self, schema, system, payload):
            calls.append(schema)
            assert payload["derivation"]["evolution_strategies"] == catalog
            assert "未请求的继承" not in str(payload)
            if schema is PlannerQueryProposal:
                assert "按冻结方法执行" not in str(payload)
                return schema(rationale="查找证据", queries=({"text": "durable lock requirement", "kinds": ("semantic_unit",), "limit": 8},))
            if schema is PlannerReadProposal:
                return schema(rationale="读取证据与方法", candidate_ids=(payload["candidates"][0]["candidate_id"],),
                    strategy_reads=({**catalog[0]["read"], "memory_id": "foreign" if foreign else "frozen", "max_bytes": 256},))
            if schema is PlannerStrategyReadProposal:
                previous = payload["strategy_read_pages"][0]
                return schema(rationale="继续读完整方法", strategy_reads=({**catalog[0]["read"], "cursor": previous["next_cursor"], "max_bytes": 256},))
            assert schema is LaneAdviceProposal
            assert "".join(item["content"] for item in payload["strategy_read_pages"]) == body["strategy"]["evolution"]
            return schema(rationale="按已读取方法判断", work_specs=())
    async def reader(requests):
        reads.extend(requests)
        if limited:
            model_config.context_window = 1024
        return tuple([await read_strategy_page(None, (snapshot,), request) for request in requests])
    async def checkpoint(current):
        checkpoints.append(current)
    async def run():
        index = _long_index()
        portfolio = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
        session = PlanningRetrievalSession.create(observation_hash="o" * 64, frontier_hash="f" * 64,
            catalog=portfolio, planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
            retrieval_version=AuthorizedSemanticRetriever.VERSION,
            budget=RetrievalBudget(max_queries=2, max_candidates=16, max_exact_reads=4, max_model_calls=12, max_tokens=1000))
        payload = {"mission": {"outcome": "验证"}, "scope": {"derivation_input": {
            "portfolio_index_catalog": portfolio.model_dump(mode="json"), "evolution_strategies": catalog}}}
        result = await RetrievalBackedCognitiveAdvisor(Model(), strategy_reader=reader, checkpoint=checkpoint).plan(payload, session, (index,))
        if foreign:
            assert result.session.state == "blocked" and not reads and len(calls) == 2
        elif limited:
            assert result.session.state == "blocked" and result.blocker_code == "provider_request_window"
            assert len(calls) == result.session.usage.model_calls == 2
        else:
            assert result.session.state == "planned" and len(reads) == len(result.session.strategy_read_pages)
            assert len(calls) == len(reads) + 2
            resumed = PlanningRetrievalSession.model_validate_json(result.session.model_dump_json())
            result2 = await RetrievalBackedCognitiveAdvisor(Model(), strategy_reader=reader).plan(payload, resumed, (index,))
            assert result2.proposal == result.proposal and len(calls) == len(reads) + 2
            assert any(item.strategy_read_pages for item in checkpoints)
    asyncio.run(run())


def test_legacy_observation_without_explicit_identity_uses_its_own_frozen_body(isolated_postgres_database, tmp_path):
    from backend.app.desktop.agent_loop.models import LoopObservation
    async def run():
        async with live(tmp_path) as (sessions, seed, memories, observations):
            observation = await observations.capture(seed["loop_id"], seed["round_id"])
            entry = observation.evolution_strategies[0]
            async with sessions.begin() as session:
                snapshot = await session.get(FrozenStrategySnapshot, entry["content_hash"])
                full = snapshot.payload
                legacy = observation.model_copy(update={"observation_id": None, "decision_inputs_ref": None,
                    "generation": 0, "input_schema_version": 0, "evolution_strategies": (full,)})
                base = await session.get(LoopObservation, observation.observation_id)
                base.envelope = legacy.model_dump(mode="json")
                base.envelope_hash = observation_hash(legacy)
            page = (await observations.selective_read(legacy, (PatrolStrategyReadRequest(**entry["read"]),)))[0]
            assert page["content"] and full["strategy"]["evolution"].startswith(page["content"])
            async with sessions() as session:
                base = await session.get(LoopObservation, observation.observation_id)
                assert base.envelope_hash == observation_hash(legacy)
    asyncio.run(run())


@pytest.mark.parametrize("read_first", [False, True])
def test_patrol_adoption_requires_a_read_of_the_frozen_version(monkeypatch, read_first):
    from backend.app.desktop.agent_loop import round_orchestration as module
    from backend.app.desktop.agent_loop.patrol_contract import PatrolContractViolation
    from test_loop_patrol_capacity_recovery import _observation

    body = {"memory_id": "adopt", "title": "方法", "workspace_id": None,
            "strategy": {"applicability": "适用", "evolution": "先验证假设", "inheritance": "有效要求"}}
    snapshot = {**body, "content_hash": content_hash(body)}
    entry = strategy_catalog((snapshot,))[0]
    observation = _observation().model_copy(update={"evolution_strategies": (entry,), "grant": {"holder_id": "holder", "grant_id": "grant"}})
    calls = []
    class Provider:
        async def ainvoke(self, messages, **kwargs):
            calls.append(messages)
            if read_first and len(calls) == 1:
                return AIMessage(content=json.dumps({"reads": [entry["read"]]}))
            return AIMessage(content=json.dumps({"rationale": "继续验证", "mission_references": [{"role": "outcome", "reference_id": "outcome"}],
                "strategy_adoptions": [{"memory_id": "adopt", "content_hash": entry["content_hash"], "reason": "验证假设", "action_indexes": [0]}],
                "actions": [{"action": "continue_context", "context_id": "context", "context_revision_id": "revision", "message": "继续"}]}))
    async def reader(observation, requests):
        return tuple([await read_strategy_page(None, (snapshot,), request) for request in requests])
    monkeypatch.setattr(module, "create_chat_model", lambda **kwargs: Provider())
    model = StructuredPatrolDecisionModel(config(), reader=reader)
    if read_first:
        intent = asyncio.run(model(observation))
        assert intent.strategy_adoptions[0].content_hash == entry["content_hash"]
        assert len(calls) == 2
    else:
        with pytest.raises(PatrolContractViolation, match="读取"):
            asyncio.run(model(observation))
        assert len(calls) == 1


@pytest.mark.parametrize("strategy_reading", [False, True])
def test_patrol_extra_steps_are_only_used_for_strategy_pages(strategy_reading):
    from backend.app.desktop.agent_loop.round_orchestration import PatrolDecisionProposal
    from test_loop_patrol_capacity_recovery import _observation
    from test_patrol_cognitive_contract import _FLAT

    body = {"memory_id": "pages", "title": "方法", "workspace_id": None,
            "strategy": {"applicability": "适用", "evolution": "x" * 1300, "inheritance": "约束"}}
    snapshot = {**body, "content_hash": content_hash(body)}
    entry = strategy_catalog((snapshot,))[0]
    observation = _observation().model_copy(update={"evolution_strategies": (entry,)})
    pages = []
    proposal = PatrolDecisionProposal.model_validate(_FLAT)
    async def reader(observation, requests):
        if strategy_reading:
            result = await read_strategy_page(None, (snapshot,), requests[0])
        else:
            result = {"context_id": "context", "messages": []}
        pages.append(result)
        return (result,)
    async def invoke(model, messages, method, schema):
        if schema is PatrolDecisionProposal:
            return proposal
        if strategy_reading:
            if pages and pages[-1]["next_cursor"] is None:
                return PatrolCognitiveStep(decision=proposal)
            return PatrolCognitiveStep(reads=(PatrolStrategyReadRequest(**{**entry["read"],
                "cursor": pages[-1]["next_cursor"] if pages else 0, "max_bytes": 256}),))
        return PatrolCognitiveStep.model_validate({"reads": [{"context_id": "context", "revision_id": "revision"}]})
    model = StructuredPatrolDecisionModel(config(), reader=reader)
    model._invoke = invoke
    result = asyncio.run(model._decide(None, [], "prompt_json", observation))
    assert result == proposal
    assert len(pages) == (6 if strategy_reading else 3)
    if strategy_reading:
        assert "".join(page["content"] for page in pages) == body["strategy"]["evolution"]
