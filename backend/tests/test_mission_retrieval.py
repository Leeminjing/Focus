r"""本文件对外提供冻结 Mission catalog、来源类型化检索与精确读取的领域回归测试。

输入为仅有问候的 Context semantic index、含远端检查项的 Mission 和冻结资源策略；输出为可分页候选、
精确 Mission 引用及 query/candidate/read 真实账本，且旧 Context 身份不被伪造。具体工作流为构造新版
planning session，以 Mission catalog identity 查询、记录候选并精读远端 section，再验证持久化重放。
示例：`pytest backend/tests/test_mission_retrieval.py -q`。
"""

import asyncio
from types import SimpleNamespace

from focus.runtime.runs.usage import ModelUsage
import pytest
from pydantic import ValidationError

from backend.app.desktop.agent_loop.context_expansion.mission_sections import FrozenMissionSectionCatalog
from backend.app.desktop.agent_loop.context_expansion.contracts import (
    CandidateEvidenceIdentity, EvidenceRequirement, ExpansionOpportunity, ResolvedEvidenceBundle, WorkContextSpec,
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.context_expansion.evidence_corpus import FrozenEvidenceCorpus, FrozenEvidenceCorpusReader
from backend.app.desktop.agent_loop.context_expansion.evidence_resolver import MultiSourceEvidenceResolver
from backend.app.desktop.agent_loop.context_expansion.compiler import DeterministicExpansionPlanCompiler
from backend.app.desktop.agent_loop.context_expansion.policy import ExpansionAdmissionPolicy
from backend.app.desktop.agent_loop.context_expansion.planner import PlannerEvidenceIdentityError, WorkerResultCognitivePlanner
from backend.app.desktop.agent_loop.context_expansion.reconciliation import WorkSpecReconciler
from backend.app.desktop.agent_loop.context_expansion.synthesis import (
    ClaimSupportAssessment, ContextSynthesisClaim, ContextSynthesisDraft, ContextSynthesisValidator,
    SynthesisSection,
)
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import DeterministicTestContextSynthesisService
from backend.app.desktop.agent_loop.context_expansion.quality_verifier import DeterministicTestContextQualityService
from backend.app.desktop.agent_loop.context_expansion.semantic_retrieval import (
    AuthorizedSemanticRetriever,
    PlanningRetrievalSession,
    PlanningRetrievalSessionController,
    PortfolioIndexCatalog,
    RetrievalBudget,
    SemanticRetrievalQuery,
)
from backend.app.desktop.agent_loop.context_expansion.retrieval_planner import (
    LaneAdviceProposal,
    PlannerQueryProposal,
    PlannerReadProposal,
    RetrievalBackedCognitiveAdvisor,
)
from backend.app.desktop.agent_loop.expansion_resource_policy import ExpansionResourcePolicy, resolve_expansion_resources
from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from backend.app.desktop.agent_loop.schemas import LoopBudgetContract
from backend.app.desktop.context_curation import MultiSourceEvidence, SourceRevisionEvidence
from backend.app.desktop.context_curation import evidence_ref_key
from backend.app.desktop.agent_loop.context_expansion.contracts import SpawnContextIntent
from backend.tests.test_complete_semantic_context_derivation import _long_index
from backend.tests.test_semantic_context_planning import _observation


def test_distant_mission_section_is_exactly_read_with_real_resource_usage() -> None:
    index = _long_index()
    checks = [
        {"check_id": f"check-{number}", "claim": f"Verify ordinary task {number}", "expected_evidence_kinds": ["test"]}
        for number in range(31)
    ]
    checks[30]["claim"] = "Verify distant mission requirement for Obsidian Vault tools"
    mission = EffectiveMissionProjector.from_rows(
        structured=SimpleNamespace(
            revision=4,
            outcome="Build the Obsidian agent plugin",
            boundaries={"in_scope": ["test Vault"], "required_invariants": [], "prohibited_actions": [], "legacy_text": None},
            completion_checks=checks,
        ),
        legacy=None,
    )
    mission_catalog = FrozenMissionSectionCatalog.from_mission("loop-1", mission)
    catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,), mission_catalog=mission_catalog)
    frozen = resolve_expansion_resources(LoopBudgetContract().as_grant_budgets(), 1)
    session = PlanningRetrievalSession.create(
        observation_hash="o" * 64,
        frontier_hash="f" * 64,
        catalog=catalog,
        planner_version="mission-test-v1",
        retrieval_version=AuthorizedSemanticRetriever.VERSION,
        budget=RetrievalBudget(
            max_queries=frozen.policy.max_queries,
            max_candidates=frozen.policy.max_unique_candidates,
            max_exact_reads=frozen.policy.max_exact_reads,
            max_model_calls=frozen.policy.max_planner_model_calls,
            max_tokens=frozen.policy.max_planner_tokens,
        ),
        frozen_resources=frozen,
    )
    query = SemanticRetrievalQuery.create(
        text="distant mission requirement Obsidian Vault tools",
        index_ids=(mission_catalog.catalog_id,),
        kinds=("mission_section",),
        limit=40,
    )
    retriever = AuthorizedSemanticRetriever(mission_catalog)
    candidates, coverage = retriever.retrieve_page(session, query, (index,))
    session = PlanningRetrievalSessionController.record_query_plan(session, (query,))
    session = PlanningRetrievalSessionController().record_query(session, query, candidates, coverage)
    selected = next(item for item in candidates if item.mission_ref and item.mission_ref.item_id == "check-30")
    read = retriever.read(session, selected, (index,))
    session = PlanningRetrievalSessionController().record_reads(session, (read,))

    assert selected.source_type == "mission" and selected.source is None
    assert read.source_type == "mission" and read.source is None
    assert read.evidence_refs == (selected.mission_ref,)
    assert read.content["claim"] == checks[30]["claim"]
    assert session.usage.queries == 1
    assert session.usage.candidates == len(candidates)
    assert session.usage.exact_reads == 1
    assert PlanningRetrievalSession.model_validate_json(session.model_dump_json()) == session


def test_planner_can_select_and_cite_distant_mission_section() -> None:
    index = _long_index()
    checks = [
        {"check_id": f"check-{number}", "claim": f"Verify routine requirement {number}", "expected_evidence_kinds": ["test"]}
        for number in range(31)
    ]
    checks[-1]["claim"] = "Verify Obsidian Vault permission boundary"
    mission = EffectiveMissionProjector.from_rows(
        structured=SimpleNamespace(
            revision=4,
            outcome="Build the Obsidian agent plugin",
            boundaries={"in_scope": ["test Vault"], "required_invariants": [], "prohibited_actions": [], "legacy_text": None},
            completion_checks=checks,
        ),
        legacy=None,
    )
    mission_catalog = FrozenMissionSectionCatalog.from_mission("loop-1", mission)
    catalog = PortfolioIndexCatalog.create(frontier_hash="f" * 64, indexes=(index,), mission_catalog=mission_catalog)
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

    class MissionModel:
        last_usage = ModelUsage(model_calls=1, input_tokens=100, output_tokens=50)

        async def invoke(self, schema, system, payload):
            if schema is PlannerQueryProposal:
                assert payload["derivation"]["portfolio_index_catalog"]["mission_catalog"]["mission_revision"] == 4
                return schema(rationale="Find Mission requirement", queries=({
                    "text": "Obsidian Vault permission boundary",
                    "index_ids": (mission_catalog.catalog_id,),
                    "kinds": ("mission_section",),
                    "limit": 40,
                },))
            if schema is PlannerReadProposal:
                candidate = next(item for item in payload["candidates"] if "Obsidian Vault permission boundary" in item["descriptor"])
                return schema(rationale="Read exact Mission section", candidate_ids=(candidate["candidate_id"],))
            assert schema is LaneAdviceProposal
            read = payload["exact_reads"][0]
            assert read["source_type"] == "mission"
            citation = next(item for item in payload["allowed_candidate_refs"] if item["candidate_id"] == read["candidate_id"])
            return schema.model_validate({
                "rationale": "Vault permission has an independent work domain",
                "work_specs": ({
                    "objective": "Implement authorized Vault tools",
                    "separation_reason": "Vault permission boundary is independent",
                    "questions": ("Which Vault actions are authorized?",),
                    "completion_criteria": ("Authorized actions pass tests",),
                    "workspace_requirement": "read_only",
                    "evidence_requirements": ({
                        "requirement_id": "vault-boundary",
                        "role": "requirement",
                        "question": "What does the Mission require?",
                        "coverage_criterion": "Exact Mission check is present",
                        "candidate_refs": (citation,),
                    },),
                },),
            })

    result = asyncio.run(RetrievalBackedCognitiveAdvisor(
        MissionModel(), retriever=AuthorizedSemanticRetriever(mission_catalog),
    ).plan({
        "mission": mission.model_payload(),
        "frontier_hash": "f" * 64,
        "scope": {"derivation_input": {"portfolio_index_catalog": catalog.model_dump(mode="json")}},
    }, session, (index,)))

    assert result.blocker_code is None
    assert result.session.state == "planned"
    assert result.proposal is not None and len(result.proposal.work_specs) == 1
    assert result.reads[0].mission_ref.item_id == "check-30"
    assert result.proposal.work_specs[0].evidence_requirements[0].candidate_refs[0].mission_ref == result.reads[0].mission_ref
    assert result.session.usage.queries == 1
    assert result.session.usage.exact_reads == 1
    assert result.session.usage.model_calls >= 3
    spec = result.proposal.work_specs[0].freeze("mission-planner-v2")
    assert WorkContextSpec.model_validate_json(spec.model_dump_json()) == spec
    assert PlanningRetrievalSession.model_validate_json(result.session.model_dump_json()) == result.session
    citation = spec.evidence_requirements[0].candidate_refs[0]
    with_error = citation.model_dump(mode="json")
    with_error["mission_ref"]["goal_revision"] = 3
    with_error["mission_ref"]["content_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="来源版本或哈希不一致"):
        EvidenceRequirement.model_validate({
            **spec.evidence_requirements[0].model_dump(mode="json"),
            "candidate_refs": (with_error,),
        })

    observation = _observation().model_copy(update={
        "loop_id": "loop-1", "goal_revision": mission.revision, "mission": mission.model_payload(),
        "authority_revision": result.session.frozen_resources.grant_revision,
        "portfolio_frontier": ({
            "context_id": index.source.context_id,
            "revision_id": index.source.revision_id,
            "checkpoint_id": index.source.checkpoint_id,
            "revision": index.source.model_dump(mode="json"),
            "content_hash": index.source_content_hash,
        },),
    })
    worker_payload = {
        "work_specs": tuple(item.model_dump(mode="json") for item in result.proposal.work_specs),
        "planning_session": result.session.model_dump(mode="json"),
        "exact_reads": tuple(item.model_dump(mode="json") for item in result.reads),
    }
    assert WorkerResultCognitivePlanner._drafts_from_payload(worker_payload, observation) == result.proposal.work_specs
    with pytest.raises(PlannerEvidenceIdentityError, match="当前 Mission"):
        WorkerResultCognitivePlanner._drafts_from_payload(
            worker_payload, observation.model_copy(update={"goal_revision": mission.revision + 1}),
        )
    with pytest.raises(ValueError, match="grant revision"):
        WorkerResultCognitivePlanner._drafts_from_payload(
            worker_payload, observation.model_copy(update={"authority_revision": observation.authority_revision + 1}),
        )
    next_catalog = FrozenMissionSectionCatalog.from_mission("loop-1", mission.model_copy(update={"revision": mission.revision + 1}))
    with pytest.raises(ValueError, match="catalog 不可用"):
        AuthorizedSemanticRetriever(next_catalog).read(result.session, result.candidates[0], (index,))
    structured = FrozenEvidenceCorpusReader._structured(observation)
    assert {item.ref for item in structured if item.ref.kind == "mission"} == {entry.ref for entry in mission_catalog.entries}
    evidence = MultiSourceEvidence(
        sources=(SourceRevisionEvidence(
            source=index.source, projection_hash="p" * 64,
            content_hash=index.source_content_hash, messages=(),
        ),),
        structured=structured,
        evidence_frontier=tuple(item.ref for item in structured),
    )
    corpus = FrozenEvidenceCorpus.create(
        evidence=evidence,
        items=FrozenEvidenceCorpusReader._items(evidence, (), {}),
    )
    opportunity = ExpansionOpportunity.create(
        loop_id="loop-1", round_id="round-1", observation_hash="a" * 64,
        work_spec=spec, manifest_sources=(index.source,),
    )
    bundle = MultiSourceEvidenceResolver().resolve(opportunity, (), corpus)
    assert isinstance(bundle, ResolvedEvidenceBundle)
    assert bundle.items[0].ref == result.reads[0].mission_ref
    assert bundle.items[0].content_hash == result.reads[0].mission_ref.content_hash
    exhausted = MultiSourceEvidenceResolver().resolve(opportunity, (), corpus, max_items=0)
    assert exhausted.code == "evidence_budget_exhausted"
    unsupported = ContextSynthesisClaim.create(
        statement="Vault tools have been implemented and tested",
        authority="confirmed",
        citations=(bundle.items[0].ref,),
        requirement_ids=("vault-boundary",),
        question_ids=(ContextSynthesisValidator.question_id(spec.questions[0]),),
    )
    with pytest.raises(ValueError, match="direct-support"):
        ContextSynthesisValidator().validate(
            spec, bundle,
            ContextSynthesisDraft(
                sections=(SynthesisSection(title="Work status", claim_ids=(unsupported.claim_id,)),),
                claims=(unsupported,),
            ),
            (ClaimSupportAssessment(
                claim_id=unsupported.claim_id, verdict="unsupported",
                citation_keys=(evidence_ref_key(bundle.items[0].ref),),
                reason="Mission describes required work, not completed work",
            ),),
            synthesizer_version="mission-test-v1",
        )
    synthesis = asyncio.run(DeterministicTestContextSynthesisService().synthesize(None, spec, bundle))
    assert synthesis.dossier is not None
    quality = asyncio.run(DeterministicTestContextQualityService().verify(spec, bundle, synthesis.dossier))
    assert quality.assessment is not None and quality.assessment.passes
    compiled = DeterministicExpansionPlanCompiler().compile(
        opportunity, SpawnContextIntent(opportunity_id=opportunity.opportunity_id), bundle,
        dossier=synthesis.dossier, quality_assessment=quality.assessment,
    )
    assert compiled.plan.evidence_frontier == bundle.evidence_frontier
    assert evidence_ref_key(bundle.items[0].ref) in {
        evidence_ref_key(ref) for item in compiled.plan.items for ref in item.sources
    }
    restricted = resolve_expansion_resources(
        LoopBudgetContract(expansion_resources=ExpansionResourcePolicy(max_exact_reads=0)).as_grant_budgets(), 1,
    )
    limited_session = PlanningRetrievalSession.create(
        observation_hash="o" * 64, frontier_hash="f" * 64, catalog=catalog,
        planner_version=RetrievalBackedCognitiveAdvisor.VERSION,
        retrieval_version=AuthorizedSemanticRetriever.VERSION,
        budget=RetrievalBudget(
            max_queries=restricted.policy.max_queries,
            max_candidates=restricted.policy.max_unique_candidates,
            max_exact_reads=restricted.policy.max_exact_reads,
            max_model_calls=restricted.policy.max_planner_model_calls,
            max_tokens=restricted.policy.max_planner_tokens,
        ),
        frozen_resources=restricted,
    )
    limited = asyncio.run(RetrievalBackedCognitiveAdvisor(
        MissionModel(), retriever=AuthorizedSemanticRetriever(mission_catalog),
    ).plan({
        "mission": mission.model_payload(),
        "frontier_hash": "f" * 64,
        "scope": {"derivation_input": {"portfolio_index_catalog": catalog.model_dump(mode="json")}},
    }, limited_session, (index,)))
    assert limited.blocker_code == "expansion_policy_limit"
    assert limited.session.reads == ()
    class NarrowWindowMissionModel(MissionModel):
        context_window_tokens = 10_000
        max_output_tokens = 8192

    narrow = asyncio.run(RetrievalBackedCognitiveAdvisor(
        NarrowWindowMissionModel(), retriever=AuthorizedSemanticRetriever(mission_catalog),
    ).plan({
        "mission": mission.model_payload(),
        "frontier_hash": "f" * 64,
        "scope": {"derivation_input": {"portfolio_index_catalog": catalog.model_dump(mode="json")}},
    }, session, (index,)))
    assert narrow.blocker_code == "provider_request_window"
    assert narrow.session.reads == ()


def test_distinct_mission_duties_remain_separate_and_no_derivation_is_not_missing_goal() -> None:
    index = _long_index()
    mission = EffectiveMissionProjector.from_rows(
        structured=SimpleNamespace(
            revision=5,
            outcome="Build the Obsidian desktop pet Agent",
            boundaries={"in_scope": ["test Vault"], "required_invariants": [], "prohibited_actions": [], "legacy_text": None},
            completion_checks=[
                {"check_id": "ui", "claim": "Verify pet dragging", "expected_evidence_kinds": ["test"]},
                {"check_id": "vault", "claim": "Verify authorized Vault tools", "expected_evidence_kinds": ["test"]},
            ],
        ),
        legacy=None,
    )
    catalog = FrozenMissionSectionCatalog.from_mission("loop-1", mission)
    specs = []
    for check_id, objective in (("ui", "Implement draggable pet"), ("vault", "Implement authorized Vault tools")):
        ref = next(entry.ref for entry in catalog.entries if entry.ref.item_id == check_id)
        entry_id = stable_expansion_hash("mission-retrieval-entry", ref.model_dump(mode="json"))
        citation = CandidateEvidenceIdentity(
            candidate_id=stable_expansion_hash("retrieval-candidate-v2", catalog.catalog_id, "mission_section", entry_id, ref.content_hash),
            index_id=catalog.catalog_id,
            entry_id=entry_id,
            entry_kind="mission_section",
            source_type="mission",
            mission_ref=ref,
            source_content_hash=ref.content_hash,
        )
        specs.append(WorkContextSpec.create(
            planner_version="mission-planner-v2",
            objective=objective,
            separation_reason=f"The {check_id} duty has independent interfaces and checks",
            questions=(f"How is {check_id} implemented?",),
            completion_criteria=(f"The {check_id} tests pass",),
            workspace_requirement="read_only",
            evidence_requirements=(EvidenceRequirement(
                requirement_id=check_id,
                role="requirement",
                question=f"What does Mission {check_id} require?",
                coverage_criterion="Exact Mission check is available",
                candidate_refs=(citation,),
            ),),
        ))
    reconciliation = WorkSpecReconciler().reconcile(tuple(specs), relations=())
    assert len(reconciliation.canonical_specs) == 2
    assert {
        spec.evidence_requirements[0].candidate_refs[0].mission_ref.item_id
        for spec in reconciliation.canonical_specs
    } == {"ui", "vault"}

    observation = _observation().model_copy(update={
        "loop_id": "loop-1", "goal_revision": mission.revision, "mission": mission.model_payload(),
        "grant": {"capabilities": ("create_lane", "continue_context"), "context_scope": (index.source.context_id,)},
        "portfolio_frontier": ({
            "context_id": index.source.context_id,
            "revision_id": index.source.revision_id,
            "checkpoint_id": index.source.checkpoint_id,
            "revision": index.source.model_dump(mode="json"),
            "content_hash": index.source_content_hash,
        },),
    })
    opportunities = tuple(ExpansionOpportunity.create(
        loop_id="loop-1", round_id=observation.round_id,
        observation_hash="a" * 64, work_spec=spec, manifest_sources=(index.source,),
    ) for spec in reconciliation.canonical_specs)
    assessment = ExpansionAdmissionPolicy().evaluate(observation, opportunities)
    assert assessment.level in {"required", "recommended"}
    assert {item.work_spec.work_spec_id for item in assessment.opportunities} == {spec.work_spec_id for spec in specs}
    empty = ExpansionAdmissionPolicy().evaluate(observation, ())
    assert empty.level == "not_applicable"
    assert all(blocker.code != "missing_goal" for blocker in empty.blockers)
