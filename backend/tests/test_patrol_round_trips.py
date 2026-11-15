"""小证据的模型往返、冻结来源、恢复与独立 Patrol 配置回归。"""

import asyncio
import json

import pytest
from pydantic import ValidationError

from focus.config.patrol_config import PatrolConfig
from focus.runtime.runs.usage import ModelUsage
from backend.app.desktop.patrol.agent_loop.context_expansion.retrieval_planner import (
    LaneAdviceProposal, PlannerQueryProposal, PlannerReadProposal, RetrievalBackedCognitiveAdvisor,
)
from backend.app.desktop.patrol.agent_loop.context_expansion.semantic_indexer import RevisionSemanticIndexer
from backend.app.desktop.patrol.agent_loop.context_expansion.semantic_retrieval import (
    AuthorizedSemanticRetriever, PlanningRetrievalSession, PortfolioIndexCatalog,
)
from backend.app.desktop.patrol.agent_loop.queries.patrol_read_view import PatrolReadView
from backend.app.desktop.patrol.agent_loop.rounds import round_orchestration as module
from backend.app.desktop.patrol.agent_loop.workers import structured_worker
from backend.tests.config_helpers import app_config_for
from backend.tests.test_complete_semantic_context_derivation import _source
from backend.tests.test_expansion_resource_policy import _new_v2_session
from backend.tests.test_patrol_cognitive_contract import _DECISION, _required_expansion_observation, _scripted_model


def _small_session():
    index = RevisionSemanticIndexer().index(source=_source(), source_content_hash="a" * 64,
        context_role="requirements", active_objective="Verify locking",
        raw_messages=({"id": "source-message", "role": "human", "content": "Require a durable lock."},))
    _, original = _new_v2_session()
    catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
    session = PlanningRetrievalSession.create(observation_hash="o" * 64, frontier_hash="f" * 64,
        catalog=catalog, planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
        retrieval_version=AuthorizedSemanticRetriever.VERSION, budget=original.budget,
        frozen_resources=original.frozen_resources)
    return index, session


class _Model:
    small_evidence_max_bytes = 16384

    def __init__(self):
        self.calls = []
        self.last_usage = ModelUsage()

    async def invoke(self, schema, system, payload):
        self.calls.append(schema)
        self.last_usage = ModelUsage(model_calls=1, input_tokens=100, output_tokens=20)
        if schema is PlannerQueryProposal:
            return schema(rationale="Find locking evidence", queries=({"text": "lock", "kinds": ("semantic_unit",), "limit": 1},))
        if schema is PlannerReadProposal:
            return schema(rationale="Read exact source", candidate_ids=(payload["candidates"][0]["candidate_id"],))
        assert schema is LaneAdviceProposal
        assert len(payload["exact_reads"]) == 1
        assert payload["exact_reads"][0]["session_id"]
        assert payload["allowed_candidate_refs"][0]["candidate_id"] == payload["exact_reads"][0]["candidate_id"]
        return schema(rationale="Existing Context is sufficient", work_specs=())


def test_small_curator_uses_one_model_call_and_records_real_reads():
    async def run():
        index, session = _small_session()
        frozen = session.model_dump_json()
        model = _Model()
        advisor = RetrievalBackedCognitiveAdvisor(model)
        result = await advisor.plan({}, session, (index,))
        assert result.session.state == "planned"
        assert model.calls == [LaneAdviceProposal]
        assert result.session.usage.model_calls == 1
        assert result.session.usage.queries == result.session.usage.exact_reads == 1
        assert result.session.usage.input_tokens == 100
        assert result.session.usage.output_tokens == 20
        assert result.reads[0].source == index.source
        assert session.model_dump_json() == frozen
        replay = await advisor.plan({}, result.session, (index,))
        assert replay.proposal == result.proposal and len(model.calls) == 1
    asyncio.run(run())


def test_production_structured_worker_uses_small_evidence_path(monkeypatch):
    async def run():
        index, session = _small_session()
        config = app_config_for("curator", None)
        config.models[0].context_window = 131072
        chat = _scripted_model(json.dumps({"rationale": "Existing Context is sufficient", "work_specs": []}))
        chat.scripted[0].usage_metadata = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        chat.scripted[0].response_metadata = {"model_name": "curator"}
        monkeypatch.setattr(structured_worker, "create_chat_model", lambda **kwargs: chat)
        worker = structured_worker.StructuredWorkerModel(config)
        result = await RetrievalBackedCognitiveAdvisor(worker).plan({}, session, (index,))
        assert result.session.state == "planned" and len(chat.received) == 1, (result.blocker_code, result.blocker_summary, worker.last_model_metadata, len(chat.received))
        assert result.session.usage.queries == result.session.usage.exact_reads == 1
        assert worker.usage.model_calls == result.session.usage.model_calls == 1
    asyncio.run(run())


@pytest.mark.parametrize("limit", [0, 1])
def test_disabled_or_oversized_evidence_keeps_selective_planning(limit):
    async def run():
        index, session = _small_session()
        model = _Model()
        model.small_evidence_max_bytes = limit
        result = await RetrievalBackedCognitiveAdvisor(model).plan({}, session, (index,))
        assert result.session.state == "planned"
        assert model.calls == [PlannerQueryProposal, PlannerReadProposal, LaneAdviceProposal]
    asyncio.run(run())


@pytest.mark.parametrize("after_model", [False, True])
def test_small_curator_checkpoint_recovery_does_not_repeat_model_calls(after_model):
    async def run():
        index, session = _small_session()
        model, saved = _Model(), []

        async def checkpoint(current):
            if bool(current.usage.model_calls) == after_model:
                saved.append(PlanningRetrievalSession.model_validate_json(current.model_dump_json()))
                raise RuntimeError("interrupted checkpoint")

        with pytest.raises(RuntimeError, match="interrupted checkpoint"):
            await RetrievalBackedCognitiveAdvisor(model, checkpoint=checkpoint).plan({}, session, (index,))
        result = await RetrievalBackedCognitiveAdvisor(model).plan({}, saved[-1], (index,))
        assert result.session.state == "planned" and model.calls == [LaneAdviceProposal]
        assert result.session.usage.model_calls == 1 and result.session.usage.exact_reads == 1
    asyncio.run(run())


def test_small_evidence_does_not_bypass_exact_read_or_candidate_limits():
    index, session = _small_session()
    advisor = RetrievalBackedCognitiveAdvisor(_Model())
    for field in ("max_exact_reads", "max_candidates", "max_queries"):
        restricted = session.model_copy(update={"budget": session.budget.model_copy(update={field: 0})})
        assert advisor._small_evidence_session({}, restricted, (index,)) is None
        assert restricted.state == "created" and restricted.usage.model_calls == 0


def test_initial_patrol_includes_current_worker_and_related_context_in_one_call(monkeypatch):
    async def run():
        observation = _required_expansion_observation().model_copy(update={
            "grant": {"grant_id": "grant-1", "holder_id": "patrol:1"}, "expansion_assessment": None,
            "portfolio_frontier": ({"context_id": "context-1", "revision_id": "revision-1"},),
            "task_delta": {"sources": [{"source_key": "delta", "context_id": "context-1", "payload": {"outcome": "latest result"}}]},
            "worker_results": ({"request_id": "worker", "round_id": "round-1", "status": "success", "result": {"advice": "current evidence"}},),
        })
        calls = []

        async def reader(frozen, requests):
            assert frozen is observation
            calls.extend(requests)
            return ({"context_id": requests[0].context_id, "revision": {"revision_id": requests[0].revision_id},
                "messages": [{"id": "m", "content": "related frozen evidence"}]},)

        model = _scripted_model(json.dumps({"decision": _DECISION}))
        monkeypatch.setattr(module, "create_chat_model", lambda **kwargs: model)
        decision = await module.StructuredPatrolDecisionModel(app_config_for("patrol-test", None), reader=reader)(observation)
        assert decision.actions[0].action == "continue_context" and len(model.received) == 1
        assert len(calls) == 1 and calls[0].revision_id == "revision-1"
        request = " ".join(str(message.content) for message in model.received[0])
        assert all(text in request for text in ("latest result", "current evidence", "related frozen evidence"))
        assert observation.worker_results[0]["result"] == {"advice": "current evidence"}
    asyncio.run(run())


def test_initial_worker_evidence_is_bounded_and_keeps_historical_directory():
    observation = _required_expansion_observation().model_copy(update={"worker_results": (
        {"request_id": "old", "round_id": "old-round", "status": "success", "result": {"data": "old"}},
        {"request_id": "large", "round_id": "round-1", "status": "success", "result": {"data": "x" * 1000}},
        {"request_id": "small", "round_id": "round-1", "status": "success", "result": {"data": "new"}},
    )})
    payload = PatrolReadView.payload(observation, initial_evidence_max_bytes=100)
    assert [item["request_id"] for item in payload["worker_source_catalog"]] == ["old", "large", "small"]
    assert ["result" in item for item in payload["worker_results"]] == [False, False, True]
    assert payload["worker_results"][-1]["result_complete"] is True


def test_patrol_model_and_output_limit_are_independent(monkeypatch):
    async def run():
        config = app_config_for("deep-index", None)
        config.models.append(config.models[0].model_copy(update={"name": "patrol-fast", "default": False, "curation_default": False}))
        config.patrol = PatrolConfig(model_name="patrol-fast", max_output_tokens=1024)
        monkeypatch.setenv("FOCUS_MODEL", "deep-index")
        model = _scripted_model(json.dumps({"decision": _DECISION}))
        options = []
        monkeypatch.setattr(module, "create_chat_model", lambda **kwargs: options.append(kwargs) or model)
        observation = _required_expansion_observation().model_copy(update={
            "grant": {"grant_id": "grant", "holder_id": "holder"}, "expansion_assessment": None})
        await module.StructuredPatrolDecisionModel(config)(observation)
        assert options[0]["name"] == "patrol-fast" and options[0]["max_tokens"] == 1024
        assert config.resolve_curation_model_name() == "deep-index"
        assert config.models[0].curation_max_output_tokens == 8192
    asyncio.run(run())


def test_patrol_final_request_schema_forbids_another_read():
    assert module.PatrolFinalStep.model_validate({"decision": _DECISION}).decision is not None
    assert module.PatrolFinalStep.model_validate({"needs_evidence": ["curator"]}).needs_evidence == ("curator",)
    with pytest.raises(ValidationError):
        module.PatrolFinalStep.model_validate({"reads": [{"context_id": "context", "revision_id": "revision"}]})


def test_initial_context_evidence_deduplicates_preview_and_stays_within_budget():
    async def run():
        observation = _required_expansion_observation().model_copy(update={
            "portfolio_frontier": ({"context_id": "context", "revision_id": "revision", "message_evidence_preview": (
                {"message_id": "recent", "content": "already provided"},)},),
            "task_delta": {"sources": [{"context_id": "context"}]},
        })
        original = ({"context_id": "context", "messages": (
            {"id": "old-large", "content": "a" * 10000},
            {"id": "old-small", "content": "additional evidence"},
            {"id": "recent", "content": "already provided"},
        )},)

        async def reader(*args):
            return original

        model = module.StructuredPatrolDecisionModel(object(), reader=reader)
        payload = PatrolReadView.payload(observation)
        await model._initial_context_evidence(observation, payload, 500)
        evidence = payload["initial_context_evidence"][0]
        assert [message["id"] for message in evidence["messages"]] == ["old-small"]
        assert evidence["history_complete"] is False and evidence["omitted_message_count"] == 2
        assert len(PatrolReadView._encoded(evidence)) <= 500
        assert len(original[0]["messages"]) == 3
    asyncio.run(run())
