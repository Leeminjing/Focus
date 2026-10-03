r"""本文件对外提供大任务 Context Expansion 资源策略的确定性回归测试。

输入为冻结的长历史索引、六条跨域检索查询、重复 evidence 命中、分页精读证据和模型实际 Token 用量；输出为候选、精读、
WorkSpec 与 blocker 的可核对断言。具体工作流为构造生产形态的检索规划及重启续跑，再检查预算核算、单请求窗口与阶段结果。
示例：`pytest backend/tests/test_expansion_resource_policy.py -q`。
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from focus.runtime.runs.usage import ModelUsage
from pydantic import ValidationError

from backend.app.desktop.agent_loop.expansion_resource_policy import (
    ExpansionResourcePolicy,
    resolve_expansion_resources,
)
from backend.app.desktop.agent_loop.expansion_usage import ExpansionUsageCharge, ExpansionUsageLedger
from backend.app.desktop.agent_loop.expansion_resource_projection import expansion_resource_view, planning_session_view, repeated_expansion_blocker
from backend.app.desktop.agent_loop.schemas import AdjustLoopBudgetsRequest, LoopBudgetContract, LoopCreateRequest

from backend.app.desktop.agent_loop.context_expansion.retrieval_planner import (
    LaneAdviceProposal,
    PlannerQueryProposal,
    PlannerReadProposal,
    RetrievalBackedCognitiveAdvisor,
)
from backend.app.desktop.agent_loop.context_expansion.candidate_paging import CandidateDescriptorPager
from backend.app.desktop.agent_loop.context_expansion.index_model_budget import IndexBudgetExceeded, IndexModelBudget
from backend.app.desktop.agent_loop.context_expansion.contracts import CognitivePlanResult, DerivationStageRecord, ExpansionOpportunity, ResolvedEvidenceBundle, stable_expansion_hash
from backend.app.desktop.agent_loop.context_expansion.coordinator import ContextExpansionCoordinator
from backend.app.desktop.agent_loop.context_expansion.evidence_corpus import CorpusEvidenceItem, FrozenEvidenceCorpus
from backend.app.desktop.agent_loop.context_expansion.evidence_resolver import MultiSourceEvidenceResolver
from backend.app.desktop.agent_loop.context_expansion.planner import WorkerResultCognitivePlanner
from backend.app.desktop.agent_loop.context_expansion.quality_verifier import DeterministicTestContextQualityService
from backend.app.desktop.agent_loop.context_expansion.signals import ExpansionSignalCollector
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import DeterministicTestContextSynthesisService
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
    AuthorizedSemanticRetriever,
    PlanningRetrievalSession,
    PlanningRetrievalSessionController,
    PortfolioIndexCatalog,
    RetrievalBudget,
    SemanticRetrievalQuery,
)
from backend.tests.test_complete_semantic_context_derivation import _long_index
from backend.tests.test_semantic_context_planning import _observation
from backend.app.desktop.context_curation import MultiSourceEvidence, NamespacedMessageRef, SourceMessageEvidence, SourceRevisionEvidence, evidence_ref_key


def test_expansion_policy_defaults_and_round_trip() -> None:
    policy = ExpansionResourcePolicy()
    assert all(value is None for key, value in policy.model_dump().items() if key != "version")
    assert ExpansionResourcePolicy.model_validate_json(policy.model_dump_json()) == policy
    assert LoopBudgetContract().expansion_resources == policy


def test_unlimited_resources_still_respect_actual_model_window() -> None:
    async def run():
        frozen = resolve_expansion_resources(LoopBudgetContract().as_grant_budgets(), 1)
        budget = IndexModelBudget(frozen)
        model = SimpleNamespace(context_window_tokens=16384, max_output_tokens=2048)
        assert budget._request_limits(model) == (14336, 2048)
        payload = {"source": "x" * 10000}
        assert budget.fits_request(model, PlannerQueryProposal, "plan", payload)
        await budget.admit(model, PlannerQueryProposal, "plan", payload)
        await budget.admit(model, PlannerQueryProposal, "plan", payload)
        assert budget._calls is None and budget._input is None and budget._output is None
        with pytest.raises(IndexBudgetExceeded, match="provider_request_window"):
            await budget.admit(model, PlannerQueryProposal, "plan", {"source": "x" * 20000})
        explicit = frozen.model_copy(update={"policy": ExpansionResourcePolicy(
            max_request_input_tokens=4096, output_token_reserve=4096,
        )})
        assert IndexModelBudget(explicit)._request_limits(model) == (4096, 4096)

    asyncio.run(run())


def test_expansion_policy_accepts_explicit_limits_and_rejects_invalid_shape() -> None:
    policy = ExpansionResourcePolicy(max_queries=2, max_unique_candidates=8, max_exact_reads=2)
    budgets = LoopBudgetContract(max_model_calls=200, expansion_resources=policy, expansion_resources_source="explicit")
    frozen = resolve_expansion_resources(budgets.model_dump(mode="json"), 3, {"model_calls": 5})
    assert frozen.policy == policy
    assert frozen.source == "explicit"
    assert frozen.grant_revision == 3
    assert frozen.global_model_calls_remaining == 195
    with pytest.raises(ValidationError):
        ExpansionResourcePolicy.model_validate({"max_queries": 2, "unknown": 1})
    with pytest.raises(ValidationError):
        ExpansionResourcePolicy(max_unique_candidates=1, max_exact_reads=2)
    with pytest.raises(ValidationError):
        LoopBudgetContract.model_validate({"expansion_resources": {"max_planner_tokens": -1}})


def test_budget_revision_preserves_previous_expansion_policy_for_legacy_clients() -> None:
    original = LoopBudgetContract(
        max_model_calls=240,
        expansion_resources=ExpansionResourcePolicy(max_queries=8),
    ).as_grant_budgets()
    revised = LoopBudgetContract(max_rounds=100).as_grant_budgets(original)
    assert revised["max_rounds"] == 100
    assert revised["max_model_calls"] == 240
    assert revised["expansion_resources"]["max_queries"] == 8
    assert revised["expansion_resources_source"] == "explicit"
    assert LoopBudgetContract(max_rounds=100).as_grant_budgets()["expansion_resources_source"] == "default"


def test_start_and_revision_api_contracts_expose_and_validate_expansion_resources() -> None:
    request = LoopCreateRequest.model_validate({
        "loop_id": "loop-1", "workspace_id": "workspace-1", "initial_context_id": "context-1",
        "initial_run_id": "run-1", "holder_id": "holder-1", "goal": "Build the plugin",
        "task_contract": "Complete all required checks", "acceptance_criteria": ({"check_id": "build"},),
        "capabilities": ("read",), "context_scope": ("context-1",), "permission_scope": (),
        "budgets": {"max_rounds": 75, "expansion_resources": {"max_queries": 24}},
    })
    initial = request.budgets.as_grant_budgets()
    assert initial["max_rounds"] == 75
    assert initial["expansion_resources"]["max_queries"] == 24
    assert initial["expansion_resources_source"] == "explicit"
    revision = AdjustLoopBudgetsRequest.model_validate({
        "command": "adjust_budgets", "budgets": {"max_rounds": 100},
    }).budgets.as_grant_budgets(initial)
    assert revision["max_rounds"] == 100
    assert revision["expansion_resources"]["max_queries"] == 24
    with pytest.raises(ValidationError):
        LoopCreateRequest.model_validate({
            **request.model_dump(mode="json"),
            "budgets": {"expansion_resources": {"max_queries": 24, "unrecognized": 1}},
        })
    with pytest.raises(ValidationError):
        AdjustLoopBudgetsRequest.model_validate({
            "command": "adjust_budgets", "budgets": {"expansion_resources": {"max_unique_candidates": 4096, "max_exact_reads": 9000}},
        })


def test_planning_session_freezes_policy_source_grant_and_global_remainder() -> None:
    index = _long_index()
    catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
    first = resolve_expansion_resources(
        LoopBudgetContract(max_model_calls=200).as_grant_budgets(),
        1,
        {"model_calls": 3, "input_tokens": 100, "output_tokens": 50},
    )
    policy = first.policy
    budget = RetrievalBudget(
        max_queries=policy.max_queries,
        max_candidates=policy.max_unique_candidates,
        max_exact_reads=policy.max_exact_reads,
        max_model_calls=policy.max_planner_model_calls,
        max_tokens=policy.max_planner_tokens,
    )
    session = PlanningRetrievalSession.create(
        observation_hash="o" * 64,
        frontier_hash="f" * 64,
        catalog=catalog,
        planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
        retrieval_version=AuthorizedSemanticRetriever.VERSION,
        budget=budget,
        frozen_resources=first,
    )
    assert session.schema_version == "retrieval-session-v2"
    assert session.frozen_resources.source == "default"
    assert session.frozen_resources.global_model_calls_remaining == 197
    assert PlanningRetrievalSession.model_validate_json(session.model_dump_json()) == session
    second = resolve_expansion_resources(
        LoopBudgetContract(expansion_resources=ExpansionResourcePolicy(max_queries=8)).as_grant_budgets(),
        2,
        {"model_calls": 4},
    )
    revised = PlanningRetrievalSession.create(
        observation_hash=session.observation_hash,
        frontier_hash=session.frontier_hash,
        catalog=catalog,
        planner_version=session.planner_version,
        retrieval_version=session.retrieval_version,
        budget=RetrievalBudget(
            max_queries=second.policy.max_queries,
            max_candidates=second.policy.max_unique_candidates,
            max_exact_reads=second.policy.max_exact_reads,
            max_model_calls=second.policy.max_planner_model_calls,
            max_tokens=second.policy.max_planner_tokens,
        ),
        frozen_resources=second,
    )
    assert revised.session_id != session.session_id
    assert session.frozen_resources.grant_revision == 1


def test_historical_blocked_session_is_read_only_while_new_round_uses_current_policy() -> None:
    _, historical = _session()
    historical = PlanningRetrievalSessionController.block(historical, "retrieval_budget_exhausted")
    stored = historical.model_dump(mode="json")
    restored = PlanningRetrievalSession.model_validate(stored)
    view = planning_session_view(stored, round_id="old-round", round_number=1)
    assert restored.schema_version == "retrieval-session-v1"
    assert view["blocker_code"] == "retrieval_budget_exhausted"
    assert view["limits"]["max_candidates"] == 64
    assert view["source"] == "historical"
    old_stage = DerivationStageRecord(
        stage="retrieval_planning", input_identities=(historical.session_id,),
        version="retrieval-backed-cognitive-advisor-v1", duration_ms=10,
        safe_summary="planning budget exhausted", failure_code="retrieval_budget_exhausted",
    )
    assert DerivationStageRecord.model_validate_json(old_stage.model_dump_json()) == old_stage
    legacy = resolve_expansion_resources({"max_model_calls": 200}, 2)
    assert legacy.source == "legacy_default"
    _, current = _new_v2_session()
    assert current.schema_version == "retrieval-session-v2"
    assert current.budget.max_candidates is None
    assert historical.model_dump(mode="json") == stored


def test_three_query_hits_charge_one_unique_evidence_entry() -> None:
    index = _long_index()
    catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
    frozen = resolve_expansion_resources(LoopBudgetContract().as_grant_budgets(), 1)
    policy = frozen.policy
    session = PlanningRetrievalSession.create(
        observation_hash="o" * 64,
        frontier_hash="f" * 64,
        catalog=catalog,
        planner_version="planner-v2",
        retrieval_version=AuthorizedSemanticRetriever.VERSION,
        budget=RetrievalBudget(
            max_queries=policy.max_queries,
            max_candidates=policy.max_unique_candidates,
            max_exact_reads=policy.max_exact_reads,
            max_model_calls=policy.max_planner_model_calls,
            max_tokens=policy.max_planner_tokens,
        ),
        frozen_resources=frozen,
    )
    retriever = AuthorizedSemanticRetriever()
    controller = PlanningRetrievalSessionController()
    queries = tuple(
        SemanticRetrievalQuery.create(text=f"durable lock requirement {domain}", kinds=("semantic_unit",), limit=1)
        for domain in ("ui", "testing", "architecture")
    )
    session = controller.record_query_plan(session, queries)
    for query in queries:
        candidates, coverage = retriever.retrieve_page(session, query, (index,))
        session = controller.record_query(session, query, candidates, coverage)
    assert len(session.query_hits) == 3
    assert len({hit.query_id for hit in session.query_hits}) == 3
    assert session.usage.candidates == 1
    assert len(session.candidates) == 1
    assert session.candidates[0].candidate_id == session.query_hits[0].candidate_id
    assert tuple(item.deduplicated for item in session.query_coverage) == (0, 1, 1)
    assert all(item.requested == 1 and item.returned == 1 and item.unvisited > 0 for item in session.query_coverage)
    assert retriever.read(session, session.candidates[0], (index,)).entry_id == session.candidates[0].entry_id
    assert PlanningRetrievalSession.model_validate_json(session.model_dump_json()) == session


def test_v2_exact_read_rejects_cross_session_and_tampered_provenance() -> None:
    index = _long_index()
    catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
    frozen = resolve_expansion_resources(LoopBudgetContract().as_grant_budgets(), 1)
    policy = frozen.policy
    budget = RetrievalBudget(
        max_queries=policy.max_queries,
        max_candidates=policy.max_unique_candidates,
        max_exact_reads=policy.max_exact_reads,
        max_model_calls=policy.max_planner_model_calls,
        max_tokens=policy.max_planner_tokens,
    )
    retriever = AuthorizedSemanticRetriever()
    controller = PlanningRetrievalSessionController()
    query = SemanticRetrievalQuery.create(text="durable lock requirement", kinds=("semantic_unit",), limit=1)
    sessions = []
    for observation_hash in ("a" * 64, "b" * 64):
        session = PlanningRetrievalSession.create(
            observation_hash=observation_hash,
            frontier_hash="f" * 64,
            catalog=catalog,
            planner_version="planner-v2",
            retrieval_version=retriever.VERSION,
            budget=budget,
            frozen_resources=frozen,
        )
        session = controller.record_query_plan(session, (query,))
        candidates, coverage = retriever.retrieve_page(session, query, (index,))
        sessions.append(controller.record_query(session, query, candidates, coverage))
    first, second = sessions
    candidate = first.candidates[0]
    assert candidate.candidate_id == second.candidates[0].candidate_id
    with pytest.raises(ValueError, match="session provenance"):
        retriever.read(second, candidate, (index,))
    with pytest.raises(ValueError, match="session provenance"):
        retriever.read(first, candidate.model_copy(update={"source_content_hash": "0" * 64}), (index,))
    with pytest.raises(ValueError, match="session scope"):
        retriever.read(first, candidate.model_copy(update={"index_id": "0" * 64}), (index,))
    with pytest.raises(ValueError, match="未在同一 session"):
        retriever.read(first, candidate.model_copy(update={"candidate_id": "0" * 64}), (index,))
    verified_read = retriever.read(first, candidate, (index,))
    with pytest.raises(ValueError, match="provenance"):
        controller.record_reads(first, (verified_read.model_copy(update={"source_content_hash": "0" * 64}),))


def test_usage_ledger_replay_is_idempotent_and_rejects_conflicting_charge() -> None:
    ledger = ExpansionUsageLedger(ledger_id="session-1")
    charge = ExpansionUsageCharge(
        operation_id="model:read-page-1",
        kind="model_call",
        input_tokens=1800,
        output_tokens=200,
    )
    ledger = ledger.record(charge)
    restored = ExpansionUsageLedger.model_validate_json(ledger.model_dump_json())
    assert restored.record(charge) == restored
    assert restored.total("model_call") == 1
    assert restored.input_tokens == 1800
    assert restored.output_tokens == 200
    with pytest.raises(ValueError, match="冲突"):
        restored.record(charge.model_copy(update={"output_tokens": 300}))


def test_failed_model_attempt_is_charged_then_restart_resumes_without_replay() -> None:
    class _RetryModel:
        def __init__(self) -> None:
            self.calls = 0
            self.last_usage = ModelUsage()

        async def invoke(self, schema, system, payload):
            self.calls += 1
            self.last_usage = ModelUsage(model_calls=1, input_tokens=1000 * self.calls, output_tokens=100)
            if self.calls == 1:
                raise RuntimeError("private provider response must not appear in audit")
            return schema(rationale="Retry succeeded", queries=({"text": "durable lock"},))

    async def run():
        index, session = _new_v2_session()
        saved = []

        async def interrupt_after_checkpoint(current):
            saved.append(PlanningRetrievalSession.model_validate_json(current.model_dump_json()))
            if len(saved) == 1:
                raise RuntimeError("process interrupted after durable checkpoint")

        model = _RetryModel()
        advisor = RetrievalBackedCognitiveAdvisor(model, checkpoint=interrupt_after_checkpoint)
        with pytest.raises(RuntimeError, match="process interrupted"):
            await advisor._invoke(session, PlannerQueryProposal, "query authority", {}, operation_id="query_plan")
        assert saved[0].usage.model_calls == 1
        assert saved[0].usage.input_tokens == 1000
        restored = saved[0]
        result, completed = await RetrievalBackedCognitiveAdvisor(model)._invoke(
            restored, PlannerQueryProposal, "query authority", {}, operation_id="query_plan"
        )
        assert result.queries[0].text == "durable lock"
        assert model.calls == 2
        assert completed.usage.model_calls == 2
        assert completed.usage.input_tokens == 3000
        assert completed.usage.output_tokens == 200
        assert len(completed.ledger.charges) == 2
        cached, same = await RetrievalBackedCognitiveAdvisor(model)._invoke(
            completed, PlannerQueryProposal, "query authority", {}, operation_id="query_plan"
        )
        assert cached == result and same == completed and model.calls == 2

    asyncio.run(run())


def test_model_provider_window_global_grant_and_policy_limits_are_distinct() -> None:
    class _ErrorModel:
        def __init__(self, *, provider_window=False, calls=1) -> None:
            self.provider_window = provider_window
            self.last_usage = ModelUsage(model_calls=calls, input_tokens=1800, output_tokens=150)
            self.calls = 0

        async def invoke(self, schema, system, payload):
            self.calls += 1
            if self.provider_window:
                error = RuntimeError("private provider request")
                error.code = "context_length_exceeded"
                raise error
            return schema(rationale="Query", queries=({"text": "durable lock"},))

    async def run():
        _, session = _new_v2_session()
        provider = _ErrorModel(provider_window=True)
        _, blocked = await RetrievalBackedCognitiveAdvisor(provider)._invoke(
            session, PlannerQueryProposal, "query authority", {}, operation_id="query_plan"
        )
        assert provider.calls == 1
        assert blocked.blocker_code == "provider_request_window"
        assert blocked.blocker_stage == "query_plan"
        assert blocked.blocker_boundary == "provider_request_window"
        assert blocked.blocker_operation_id == "query_plan"
        assert "private" not in blocked.blocker_summary
        assert blocked.usage.input_tokens == 1800
        _, global_session = _new_v2_session(global_model_calls=1)
        global_model = _ErrorModel(calls=2)
        _, global_blocked = await RetrievalBackedCognitiveAdvisor(global_model)._invoke(
            global_session, PlannerQueryProposal, "query authority", {}, operation_id="query_plan"
        )
        assert global_blocked.blocker_code == "global_grant_exhausted"
        assert global_blocked.blocker_boundary == "global_model_calls"
        assert global_blocked.usage.model_calls == 2
        _, policy_session = _new_v2_session(policy=ExpansionResourcePolicy(max_planner_model_calls=1))
        policy_model = _ErrorModel(calls=2)
        _, policy_blocked = await RetrievalBackedCognitiveAdvisor(policy_model)._invoke(
            policy_session, PlannerQueryProposal, "query authority", {}, operation_id="query_plan"
        )
        assert policy_blocked.blocker_code == "expansion_policy_limit"
        assert policy_blocked.blocker_boundary == "max_planner_model_calls"
        assert policy_blocked.usage.model_calls == 2

    asyncio.run(run())


def test_missing_provider_usage_is_not_misreported_as_zero_actual_tokens() -> None:
    class _UnreportedModel:
        last_usage = ModelUsage(model_calls=1)
        last_usage_reported = False

        async def invoke(self, schema, system, payload):
            return schema(rationale="Query", queries=({"text": "durable lock"},))

    async def run():
        _, session = _new_v2_session()
        _, blocked = await RetrievalBackedCognitiveAdvisor(_UnreportedModel())._invoke(
            session, PlannerQueryProposal, "query authority", {}, operation_id="query_plan"
        )
        assert blocked.blocker_code == "model_usage_unavailable"
        assert blocked.usage.model_calls == 1
        assert blocked.usage.unreported_model_calls == 1
        assert blocked.ledger.unreported_model_calls == 1
        assert blocked.ledger.charges[0].usage_reported is False

    asyncio.run(run())


def test_consecutive_internal_blocker_is_visible_despite_successful_primary_runs() -> None:
    sessions = (
        {"round_number": 3, "blocker_code": "expansion_policy_limit", "blocker_boundary": "max_queries", "source_context_ids": ("primary",), "required_expansion_evaluation": True, "stage": "query_plan", "blocker_summary": "query ceiling"},
        {"round_number": 4, "blocker_code": "expansion_policy_limit", "blocker_boundary": "max_queries", "source_context_ids": ("primary",), "required_expansion_evaluation": True, "stage": "query_plan", "blocker_summary": "query ceiling", "primary_run_status": "success"},
    )
    repeated = repeated_expansion_blocker(sessions)
    assert repeated["consecutive_rounds"] == 2
    assert repeated["code"] == "expansion_policy_limit"
    assert repeated["boundary"] == "max_queries"
    assert repeated["last_round"] == 4
    assert repeated_expansion_blocker((sessions[0], {**sessions[1], "blocker_code": None})) is None
    assert repeated_expansion_blocker((sessions[0], {**sessions[1], "blocker_boundary": "max_planner_tokens"})) is None
    assert repeated_expansion_blocker((sessions[0], {**sessions[1], "source_context_ids": ("other",)})) is None
    assert repeated_expansion_blocker((sessions[0], {**sessions[1], "required_expansion_evaluation": False})) is None
    assert repeated_expansion_blocker((sessions[0], {**sessions[1], "round_number": 5})) is None
    assert repeated_expansion_blocker((sessions[0], {**sessions[1], "blocker_code": "provider_request_window"})) is None
    assert repeated_expansion_blocker((sessions[0], {**sessions[1], "blocker_code": "expansion_policy_limit"}, {**sessions[1], "source_context_ids": ("other",)})) == repeated
    assert repeated_expansion_blocker((*sessions, {**sessions[1], "blocker_code": None, "state": "planned"})) is None


def test_coordinator_keeps_resource_failure_blocked_and_empty_work_nonbudget() -> None:
    class _EmptyProjector:
        VERSION = "empty-projection-fixture-v1"

        def project(self, observation):
            return ()

    class _NoWorkPlanner:
        VERSION = "no-work-fixture-v1"

        async def plan(self, observation, signals, manifests):
            return CognitivePlanResult()

    async def run():
        source = _observation()
        blocked_observation = source.model_copy(update={
            "worker_results": ({
                "kind": "lane_curator", "status": "success",
                "result": {"planning_blocker": {"code": "expansion_policy_limit", "summary": "Frozen query ceiling reached"}},
            },),
        })
        blocked = await ContextExpansionCoordinator(projector=_EmptyProjector()).assess(blocked_observation)
        assert blocked.level == "blocked"
        assert blocked.blockers[0].code == "expansion_policy_limit"
        assert any(item.stage == "cognitive_planning" and item.failure_code == "expansion_policy_limit" for item in blocked.stage_records)
        empty = await ContextExpansionCoordinator(projector=_EmptyProjector(), planner=_NoWorkPlanner()).assess(source)
        assert empty.level == "not_applicable"
        assert all(item.code != "expansion_policy_limit" for item in empty.blockers)

    asyncio.run(run())


def test_resource_snapshot_and_audit_replay_share_frozen_limits_and_context_identity() -> None:
    _, session = _new_v2_session(policy=ExpansionResourcePolicy(max_unique_candidates=8000))
    session = PlanningRetrievalSessionController.block(
        session, "expansion_policy_limit", summary="work spec token ceiling",
        stage="work_spec", boundary="max_planner_tokens", operation_id="work_page:page-1",
    )
    raw = session.model_dump(mode="json")
    audit = planning_session_view(raw, round_id="round-4", round_number=4)
    view = expansion_resource_view(
        LoopBudgetContract(expansion_resources=session.frozen_resources.policy).as_grant_budgets(),
        1,
        {"model_calls": 2, "input_tokens": 5000},
        (audit,),
    )
    assert view["latest_session"] == audit
    assert view["effective"]["policy"] == raw["frozen_resources"]["policy"]
    assert view["effective"]["global_model_calls_remaining"] is None
    assert view["latest_session"]["remaining"]["planner_tokens"] is None
    assert view["latest_session"]["remaining"]["unique_candidates"] == 8000
    assert view["latest_session"]["source_context_ids"] == raw["source_context_ids"]
    assert view["latest_session"]["stage"] == "work_spec"
    assert view["latest_session"]["blocker_boundary"] == "max_planner_tokens"
    assert view["latest_session"]["blocker_operation_id"] == "work_page:page-1"
    assert view["latest_session"]["required_expansion_evaluation"] is True
    assert view["latest_session"]["blocker_code"] == "expansion_policy_limit"
    assert planning_session_view(PlanningRetrievalSession.model_validate(raw).model_dump(mode="json"), round_id="round-4", round_number=4) == audit


def test_explicit_low_policy_rejects_whole_query_plan_before_retrieval() -> None:
    async def run():
        index = _long_index()
        catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
        frozen = resolve_expansion_resources(
            LoopBudgetContract(
                expansion_resources=ExpansionResourcePolicy(max_queries=6, max_unique_candidates=64, max_exact_reads=24)
            ).as_grant_budgets(),
            2,
        )
        policy = frozen.policy
        session = PlanningRetrievalSession.create(
            observation_hash="o" * 64,
            frontier_hash="f" * 64,
            catalog=catalog,
            planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
            retrieval_version=AuthorizedSemanticRetriever.VERSION,
            budget=RetrievalBudget(
                max_queries=policy.max_queries,
                max_candidates=policy.max_unique_candidates,
                max_exact_reads=policy.max_exact_reads,
                max_model_calls=policy.max_planner_model_calls,
                max_tokens=policy.max_planner_tokens,
            ),
            frozen_resources=frozen,
        )
        result = await RetrievalBackedCognitiveAdvisor(_LargeTaskQueryModel(1000)).plan(
            {"mission": {"outcome": "Build a multi-domain plugin"}}, session, (index,)
        )
        assert result.blocker_code == "expansion_policy_limit"
        assert result.session.blocker_stage == "query_plan"
        assert result.session.blocker_boundary == "max_unique_candidates"
        assert result.session.usage.queries == 0
        assert result.session.usage.candidates == 0
        assert result.session.usage.exact_reads == 0
        assert result.session.usage.model_calls == 1

    asyncio.run(run())


def test_default_policy_completes_six_query_large_history_planning() -> None:
    class _LargeTaskPlanningModel(_LargeTaskQueryModel):
        async def invoke(self, schema, system, payload):
            if schema is PlannerQueryProposal:
                return await super().invoke(schema, system, payload)
            self.last_usage = ModelUsage(model_calls=1, input_tokens=2000, output_tokens=200)
            if schema is PlannerReadProposal:
                matching = next(item for item in payload["candidates"] if "durable lock" in item["descriptor"])
                return schema(rationale="Read the frozen requirement.", candidate_ids=(matching["candidate_id"],))
            assert schema is LaneAdviceProposal
            return schema.model_validate(
                {
                    "rationale": "The runtime requirement deserves an independent investigation.",
                    "work_specs": (
                        {
                            "objective": "Analyze durable lock behavior.",
                            "separation_reason": "The causal investigation has an independent outcome.",
                            "questions": ("Why did the lock fail?",),
                            "completion_criteria": ("Frozen evidence supports a diagnosis.",),
                            "workspace_requirement": "read_only",
                            "evidence_requirements": (
                                {
                                    "requirement_id": "lock-requirement",
                                    "role": "requirement",
                                    "question": "What did the historical lock requirement say?",
                                    "coverage_criterion": "The exact frozen unit is available.",
                                    "candidate_unit_ids": (payload["allowed_candidate_unit_ids"][0],),
                                },
                            ),
                        },
                    ),
                }
            )

    async def run():
        index = _long_index()
        catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
        frozen = resolve_expansion_resources(LoopBudgetContract().as_grant_budgets(), 1)
        policy = frozen.policy
        session = PlanningRetrievalSession.create(
            observation_hash="o" * 64,
            frontier_hash="f" * 64,
            catalog=catalog,
            planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
            retrieval_version=AuthorizedSemanticRetriever.VERSION,
            budget=RetrievalBudget(
                max_queries=policy.max_queries,
                max_candidates=policy.max_unique_candidates,
                max_exact_reads=policy.max_exact_reads,
                max_model_calls=policy.max_planner_model_calls,
                max_tokens=policy.max_planner_tokens,
            ),
            frozen_resources=frozen,
        )
        result = await RetrievalBackedCognitiveAdvisor(_LargeTaskPlanningModel(25_000)).plan(
            {"mission": {"outcome": "Build a multi-domain plugin"}, "frontier_hash": "f" * 64},
            session,
            (index,),
        )
        assert result.session.state == "planned"
        assert result.proposal is not None and result.proposal.work_specs
        assert result.session.usage.queries == 6
        assert len(result.session.query_hits) > 64
        assert result.session.usage.candidates < len(result.session.query_hits)
        assert result.session.usage.tokens > 24_000
        assert result.session.usage.exact_reads > 0
        work_spec = result.proposal.work_specs[0].freeze("retrieval-worker-cognitive-planner-v2")
        opportunity = ExpansionOpportunity.create(
            loop_id="large-loop", round_id="large-round", observation_hash="a" * 64,
            work_spec=work_spec, manifest_sources=(index.source,),
        )
        source_messages = tuple(
            SourceMessageEvidence(
                ref=NamespacedMessageRef(source=index.source, message_id=item.message_id),
                role=item.role, content=item.content,
            )
            for item in index.messages
        )
        evidence = MultiSourceEvidence(sources=(SourceRevisionEvidence(
            source=index.source, projection_hash="p" * 64,
            content_hash=index.source_content_hash, messages=source_messages,
        ),))
        selected_refs = {evidence_ref_key(ref) for read in result.reads for ref in read.evidence_refs}
        corpus = FrozenEvidenceCorpus.create(
            evidence=evidence,
            items=tuple(CorpusEvidenceItem(
                ref=message.ref,
                content_hash=stable_expansion_hash("corpus-item", message.content),
                content=message.content,
                semantic_roles=("requirement",) if evidence_ref_key(message.ref) in selected_refs else (),
                semantic_unit_ids=tuple(read.entry_id for read in result.reads if message.ref in read.evidence_refs),
            ) for message in source_messages),
        )
        bundle = MultiSourceEvidenceResolver().resolve(
            opportunity, result.manifests, corpus, max_items=policy.max_compiled_evidence_items,
        )
        assert isinstance(bundle, ResolvedEvidenceBundle)
        synthesis = await DeterministicTestContextSynthesisService().synthesize(None, work_spec, bundle)
        assert synthesis.dossier is not None
        quality = await DeterministicTestContextQualityService().verify(work_spec, bundle, synthesis.dossier)
        assert quality.assessment is not None and quality.assessment.passes

    asyncio.run(run())


def test_large_evidence_pages_and_cites_distant_reads() -> None:
    class _PagingModel:
        def __init__(self) -> None:
            self.last_usage = ModelUsage()
            self.page_inputs = []
            self.work_inputs = []

        async def invoke(self, schema, system, payload):
            self.last_usage = ModelUsage(model_calls=1, input_tokens=1000, output_tokens=100)
            if schema is PlannerQueryProposal:
                return schema(rationale="Find the full history.", queries=({"text": "durable lock", "kinds": ("semantic_unit",), "limit": 32},))
            if schema is PlannerReadProposal:
                self.page_inputs.append(payload)
                return schema(
                    rationale="Retain distant exact units from this page.",
                    candidate_ids=(payload["candidates"][0]["candidate_id"], payload["candidates"][-1]["candidate_id"]),
                )
            assert schema is LaneAdviceProposal
            self.work_inputs.append(payload)
            units = payload["allowed_candidate_unit_ids"]
            return schema.model_validate(
                {
                    "rationale": "Distant evidence jointly supports the investigation.",
                    "work_specs": (
                        {
                            "objective": "Investigate durable lock history across all evidence pages.",
                            "separation_reason": "An independent evidence trail is required.",
                            "questions": ("What changed?",),
                            "completion_criteria": ("The historical cause is supported.",),
                            "workspace_requirement": "read_only",
                            "evidence_requirements": (
                                {
                                    "requirement_id": "distant-history",
                                    "role": "requirement",
                                    "question": f"What does evidence page {payload['evidence_page']['ordinal']} say?",
                                    "coverage_criterion": "Both exact units are present.",
                                    "candidate_unit_ids": units,
                                },
                            ),
                        },
                    ),
                }
            )

    async def run():
        index = _long_index()
        catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
        policy = ExpansionResourcePolicy(max_request_input_tokens=11500)
        frozen = resolve_expansion_resources(LoopBudgetContract(expansion_resources=policy).as_grant_budgets(), 1)
        session = PlanningRetrievalSession.create(
            observation_hash="o" * 64,
            frontier_hash="f" * 64,
            catalog=catalog,
            planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
            retrieval_version=AuthorizedSemanticRetriever.VERSION,
            budget=RetrievalBudget(
                max_queries=policy.max_queries,
                max_candidates=policy.max_unique_candidates,
                max_exact_reads=policy.max_exact_reads,
                max_model_calls=policy.max_planner_model_calls,
                max_tokens=policy.max_planner_tokens,
            ),
            frozen_resources=frozen,
        )
        model = _PagingModel()
        result = await RetrievalBackedCognitiveAdvisor(model).plan(
            {"mission": {"outcome": "Investigate a long history"}}, session, (index,)
        )
        assert result.session.state == "planned", (
            result.blocker_code, result.blocker_summary,
            [len(item.model_dump_json()) for item in result.session.reads],
        )
        assert model.page_inputs
        assert result.session.read_page_cursor == len(model.page_inputs)
        assert result.session.usage.exact_reads >= 2
        assert all(item["candidate_page"]["estimated_input_tokens"] <= 11500 for item in model.page_inputs)
        assert len(model.work_inputs) > 1
        assert all(len(item["exact_reads"]) == 1 for item in model.work_inputs)
        assert len(result.proposal.work_specs) == 1
        assert len(result.proposal.work_specs[0].evidence_requirements) == len(model.work_inputs)
        assert len({item.requirement_id for item in result.proposal.work_specs[0].evidence_requirements}) == len(model.work_inputs)
        assert all(len(item.candidate_unit_ids) == 1 for item in result.proposal.work_specs[0].evidence_requirements)
        tighter_pages = CandidateDescriptorPager().pages(
            result.session, result.session.candidates, {"mission": {"outcome": "Investigate a long history"}},
            context_window_tokens=13392, max_output_tokens=8192,
        )
        assert len(tighter_pages) > 1
        cited = {
            unit_id
            for requirement in result.proposal.work_specs[0].evidence_requirements
            for unit_id in requirement.candidate_unit_ids
        }
        assert {result.reads[0].entry_id, result.reads[-1].entry_id}.issubset(cited)

    asyncio.run(run())


def test_restart_continues_frozen_pages_and_cached_work_spec_without_duplicate_calls() -> None:
    class _RecoveryModel:
        def __init__(self) -> None:
            self.last_usage = ModelUsage()
            self.operations = []

        async def invoke(self, schema, system, payload):
            self.last_usage = ModelUsage(model_calls=1, input_tokens=1200, output_tokens=100)
            if schema is PlannerQueryProposal:
                self.operations.append("query")
                return schema(rationale="Find frozen history", queries=({"text": "durable lock", "kinds": ("semantic_unit",), "limit": 32},))
            if schema is PlannerReadProposal:
                page_id = payload["candidate_page"]["page_id"]
                self.operations.append(page_id)
                return schema(
                    rationale="Keep distant exact support",
                    candidate_ids=(payload["candidates"][0]["candidate_id"], payload["candidates"][-1]["candidate_id"]),
                )
            self.operations.append(payload["evidence_page"]["page_id"])
            return schema.model_validate({
                "rationale": "Independent investigation", "work_specs": ({
                    "objective": "Investigate durable lock history",
                    "separation_reason": "Independent evidence trail",
                    "questions": ("What changed?",),
                    "completion_criteria": ("Frozen evidence supports an answer.",),
                    "workspace_requirement": "read_only",
                    "evidence_requirements": ({
                        "requirement_id": "history", "role": "requirement",
                        "question": "What did the historical requirement say?",
                        "coverage_criterion": "Exact unit was read.",
                        "candidate_unit_ids": (payload["allowed_candidate_unit_ids"][0],),
                    },),
                },),
            })

    async def run():
        index, original = _new_v2_session(policy=ExpansionResourcePolicy(max_request_input_tokens=11500))
        model = _RecoveryModel()
        first_checkpoint = []

        async def interrupt_after_page(session):
            first_checkpoint.append(PlanningRetrievalSession.model_validate_json(session.model_dump_json()))
            if session.read_page_cursor == 1:
                raise RuntimeError("restart after first read page")

        with pytest.raises(RuntimeError, match="restart after first read page"):
            await RetrievalBackedCognitiveAdvisor(model, checkpoint=interrupt_after_page).plan({}, original, (index,))
        resumed = first_checkpoint[-1]
        assert resumed.session_id == original.session_id
        assert resumed.read_page_cursor == 1
        assert resumed.read_pages
        first_page_id = resumed.read_pages[0].page_id
        assert model.operations.count(first_page_id) == 1
        second_checkpoint = []

        async def interrupt_after_work(session):
            second_checkpoint.append(PlanningRetrievalSession.model_validate_json(session.model_dump_json()))
            if any(key == "work_spec" or key.startswith("work_page:") for key in session.model_results) and session.state != "planned":
                raise RuntimeError("restart after final model result")

        with pytest.raises(RuntimeError, match="restart after final model result"):
            await RetrievalBackedCognitiveAdvisor(model, checkpoint=interrupt_after_work).plan({}, resumed, (index,))
        before_final = second_checkpoint[-1]
        assert before_final.session_id == original.session_id
        assert before_final.read_page_cursor == len(before_final.read_pages)
        calls_before = len(model.operations)
        final = await RetrievalBackedCognitiveAdvisor(model).plan({}, before_final, (index,))
        assert final.session.state == "planned"
        assert final.proposal is not None and final.proposal.work_specs
        assert len(model.operations) >= calls_before
        assert all(model.operations.count(item) == 1 for item in model.operations)
        assert model.operations.count(first_page_id) == 1
        assert final.session.usage.exact_reads >= 2
        assert all(item.source_content_hash == index.source_content_hash for item in final.reads)

    asyncio.run(run())


def test_worker_work_spec_requires_exact_reads_from_same_frozen_v2_session() -> None:
    class _OneUnitModel:
        def __init__(self) -> None:
            self.last_usage = ModelUsage()

        async def invoke(self, schema, system, payload):
            self.last_usage = ModelUsage(model_calls=1, input_tokens=1500, output_tokens=100)
            if schema is PlannerQueryProposal:
                return schema(rationale="Find one frozen unit", queries=({"text": "durable lock", "kinds": ("semantic_unit",), "limit": 1},))
            if schema is PlannerReadProposal:
                return schema(rationale="Read the source", candidate_ids=(payload["candidates"][0]["candidate_id"],))
            return schema.model_validate({
                "rationale": "Independent analysis", "work_specs": ({
                    "objective": "Analyze the locked requirement",
                    "separation_reason": "It has independent evidence and outcome.",
                    "questions": ("What was required?",), "completion_criteria": ("Cite the frozen unit.",),
                    "workspace_requirement": "read_only", "required": True,
                    "evidence_requirements": ({
                        "requirement_id": "lock", "role": "requirement", "question": "What was required?",
                        "coverage_criterion": "Exact frozen unit is cited.",
                        "candidate_unit_ids": (payload["allowed_candidate_unit_ids"][0],),
                    },),
                },),
            })

    async def run():
        index, session = _new_v2_session()
        result = await RetrievalBackedCognitiveAdvisor(_OneUnitModel()).plan({}, session, (index,))
        assert result.session.state == "planned"
        source = _observation()
        frontier = dict(source.portfolio_frontier[0])
        frontier.update({
            "context_id": index.source.context_id, "revision_id": index.source.revision_id,
            "revision": index.source.model_dump(mode="json"), "content_hash": index.source_content_hash,
        })
        payload = {
            **result.proposal.model_dump(mode="json"),
            "planning_session": result.session.model_dump(mode="json"),
            "exact_reads": tuple(item.model_dump(mode="json") for item in result.reads),
        }
        observation = source.model_copy(update={
            "authority_revision": 1, "portfolio_frontier": (frontier,),
            "worker_results": ({"kind": "lane_curator", "status": "success", "result": payload},),
        })
        signals = ExpansionSignalCollector().collect(observation)
        valid = await WorkerResultCognitivePlanner().plan(observation, signals, result.manifests)
        assert valid.failure is None and len(valid.work_specs) == 1
        invented = copy.deepcopy(payload)
        invented["work_specs"][0]["evidence_requirements"][0]["candidate_unit_ids"] = ("0" * 64,)
        invalid_observation = observation.model_copy(update={
            "worker_results": ({"kind": "lane_curator", "status": "success", "result": invented},),
        })
        rejected = await WorkerResultCognitivePlanner().plan(invalid_observation, signals, result.manifests)
        assert rejected.failure is not None
        assert rejected.failure.code == "planner_evidence_identity_unknown"
        missing_read = {**payload, "exact_reads": ()}
        missing_observation = observation.model_copy(update={
            "worker_results": ({"kind": "lane_curator", "status": "success", "result": missing_read},),
        })
        rejected_missing = await WorkerResultCognitivePlanner().plan(missing_observation, signals, result.manifests)
        assert rejected_missing.failure is not None
        assert rejected_missing.failure.code == "planner_contract_invalid"

    asyncio.run(run())


class _LargeTaskQueryModel:
    def __init__(self, input_tokens: int) -> None:
        self.last_usage = ModelUsage()
        self._input_tokens = input_tokens

    async def invoke(self, schema, system, payload):
        self.last_usage = ModelUsage(model_calls=1, input_tokens=self._input_tokens, output_tokens=100)
        if schema is PlannerQueryProposal:
            return schema(
                rationale="Six independent domains share a long historical source.",
                queries=tuple(
                    {"text": f"domain {domain} requirement", "kinds": ("semantic_unit",), "limit": 16}
                    for domain in ("ui", "runtime", "model", "tools", "persistence", "testing")
                ),
            )
        raise AssertionError("the old production policy should block before exact-read selection")


def _new_v2_session(*, policy: ExpansionResourcePolicy | None = None, global_model_calls: int = 200):
    index = _long_index()
    catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
    contract = LoopBudgetContract(
        max_model_calls=global_model_calls,
        expansion_resources=policy or ExpansionResourcePolicy(),
    )
    frozen = resolve_expansion_resources(contract.as_grant_budgets(), 1)
    budget = RetrievalBudget(
        max_queries=frozen.policy.max_queries,
        max_candidates=frozen.policy.max_unique_candidates,
        max_exact_reads=frozen.policy.max_exact_reads,
        max_model_calls=frozen.policy.max_planner_model_calls,
        max_tokens=frozen.policy.max_planner_tokens,
    )
    return index, PlanningRetrievalSession.create(
        observation_hash="o" * 64,
        frontier_hash="f" * 64,
        catalog=catalog,
        planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
        retrieval_version=AuthorizedSemanticRetriever.VERSION,
        budget=budget,
        frozen_resources=frozen,
    )


def _session():
    index = _long_index()
    catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,))
    session = PlanningRetrievalSession.create(
        observation_hash="o" * 64,
        frontier_hash="f" * 64,
        catalog=catalog,
        planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
        retrieval_version=AuthorizedSemanticRetriever.VERSION,
        budget=RetrievalBudget(
            max_queries=6,
            max_candidates=64,
            max_exact_reads=24,
            max_model_calls=3,
            max_tokens=24_000,
        ),
    )
    return index, session


def test_baseline_large_task_repeated_hits_exhaust_old_candidate_budget() -> None:
    async def run():
        index, session = _session()
        result = await RetrievalBackedCognitiveAdvisor(_LargeTaskQueryModel(20_000)).plan(
            {"mission": {"outcome": "Build a multi-domain plugin"}, "frontier_hash": "f" * 64},
            session,
            (index,),
        )
        assert result.blocker_code == "retrieval_budget_exhausted"
        assert result.session.usage.candidates == 64
        assert result.session.usage.exact_reads == 0
        assert result.proposal is None
        assert len(result.session.queries) == 5

    asyncio.run(run())


def test_baseline_large_task_planning_tokens_exhaust_old_token_budget() -> None:
    async def run():
        index, session = _session()
        result = await RetrievalBackedCognitiveAdvisor(_LargeTaskQueryModel(25_000)).plan(
            {"mission": {"outcome": "Build a multi-domain plugin"}, "frontier_hash": "f" * 64},
            session,
            (index,),
        )
        assert result.blocker_code == "retrieval_budget_exhausted"
        assert result.session.usage.tokens == 25_100
        assert result.session.usage.exact_reads == 0
        assert result.proposal is None

    asyncio.run(run())
