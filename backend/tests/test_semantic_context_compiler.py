r"""本文件对外提供 claim-level dossier、三维 quality gate 与 deterministic semantic Context compiler 的纯测试。

输入为冻结 R3/R8/R5/F2 bundle、validated dossier、passing/missing assessment 与 identity-only Patrol intent；输出为固定四层 plan、
稳定 definition hash、原始证据保留和 fail-closed compilation 断言。具体工作流为通过显式 deterministic test adapters 构造
validated package，再调用纯 compiler，不连接数据库或发布 Portfolio。示例：`pytest backend/tests/test_semantic_context_compiler.py -q`。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.desktop.agent_loop.context_expansion.compiler import (
    DeterministicExpansionPlanCompiler,
)
from backend.app.desktop.agent_loop.context_expansion.contracts import (
    EvidenceRequirement,
    ExpansionOpportunity,
    ResolvedEvidenceBundle,
    ResolvedEvidenceItem,
    SpawnContextIntent,
    WorkContextSpec,
)
from backend.app.desktop.agent_loop.context_expansion.quality_verifier import (
    DeterministicTestContextQualityService,
)
from backend.app.desktop.agent_loop.context_expansion.synthesis import (
    ClaimSupportAssessment,
    ContextSynthesisClaim,
    ContextSynthesisDraft,
    ContextSynthesisValidator,
    SynthesisSection,
)
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import (
    DeterministicTestContextSynthesisService,
)
from backend.app.desktop.context_curation import (
    ComposeMessage,
    CopyMessage,
    ToolExchange,
    evidence_ref_key,
)
from backend.tests._semantic_context_fixtures import (
    failure_analysis_fixture,
    revision_ref,
    single_source_fixture,
)


def _inputs():
    spec, evidence, items = failure_analysis_fixture()
    opportunity = ExpansionOpportunity.create(loop_id="loop-semantic", round_id="round-semantic", observation_hash="a" * 64, work_spec=spec, manifest_sources=tuple(source.source for source in evidence.sources))
    bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=items)
    return opportunity, bundle, SpawnContextIntent(opportunity_id=opportunity.opportunity_id)


async def _verified(spec, bundle):
    synthesis = await DeterministicTestContextSynthesisService().synthesize(None, spec, bundle)
    assert synthesis.dossier is not None
    quality = await DeterministicTestContextQualityService().verify(spec, bundle, synthesis.dossier)
    assert quality.assessment is not None
    return synthesis.dossier, quality.assessment


def test_compiler_orders_contract_ledger_dossier_before_primary_evidence() -> None:
    async def run():
        opportunity, bundle, intent = _inputs()
        dossier, quality = await _verified(opportunity.work_spec, bundle)
        compiled = DeterministicExpansionPlanCompiler().compile(opportunity, intent, bundle, dossier=dossier, quality_assessment=quality)
        assert not hasattr(compiled, "code")
        assert isinstance(compiled.plan.items[0], ComposeMessage)
        assert compiled.plan.items[0].content.startswith("Work Contract")
        assert compiled.plan.items[1].content.startswith("Evidence Ledger")
        assert compiled.plan.items[2].content.startswith("Evidence-grounded Context Dossier")
        assert any(isinstance(item, CopyMessage) for item in compiled.plan.items[3:])
        assert any(isinstance(item, ToolExchange) for item in compiled.plan.items[3:])
        assert compiled.quality_assessment_id == quality.assessment_id

    asyncio.run(run())


def test_raw_primary_evidence_remains_present_beside_synthesized_claims() -> None:
    async def run():
        opportunity, bundle, intent = _inputs()
        dossier, quality = await _verified(opportunity.work_spec, bundle)
        compiled = DeterministicExpansionPlanCompiler().compile(opportunity, intent, bundle, dossier=dossier, quality_assessment=quality)
        assert any("account remained unlocked" in getattr(item, "content", "") for item in compiled.plan.items)
        assert any(isinstance(item, CopyMessage) for item in compiled.plan.items)

    asyncio.run(run())


def test_unknown_claim_citation_is_rejected() -> None:
    async def run():
        opportunity, bundle, _ = _inputs()
        dossier, _ = await _verified(opportunity.work_spec, bundle)
        claim = dossier.claims[0]
        message_ref = next(ref for ref in bundle.evidence_frontier if hasattr(ref, "message_id"))
        unknown = message_ref.model_copy(update={"message_id": "unknown"})
        invalid = ContextSynthesisClaim.create(statement=claim.statement, authority="confirmed", citations=(unknown,), requirement_ids=claim.requirement_ids, question_ids=claim.question_ids)
        draft = ContextSynthesisDraft(sections=(SynthesisSection(title="Invalid", claim_ids=(invalid.claim_id,)),), claims=(invalid,))
        support = ClaimSupportAssessment(claim_id=invalid.claim_id, verdict="supported", citation_keys=(evidence_ref_key(unknown),), reason="fixture")
        with pytest.raises(ValueError, match="bundle"):
            ContextSynthesisValidator().validate(opportunity.work_spec, bundle, draft, (support,), synthesizer_version="test")

    asyncio.run(run())


def test_missing_synthesis_or_quality_cannot_compile() -> None:
    opportunity, bundle, intent = _inputs()
    blocked = DeterministicExpansionPlanCompiler().compile(opportunity, intent, bundle)
    assert blocked.code == "compiler_failed"
    assert "dossier" in blocked.summary


def test_compiler_definition_is_stable_for_identical_verified_inputs() -> None:
    async def run():
        opportunity, bundle, intent = _inputs()
        dossier, quality = await _verified(opportunity.work_spec, bundle)
        compiler = DeterministicExpansionPlanCompiler()
        first = compiler.compile(opportunity, intent, bundle, dossier=dossier, quality_assessment=quality)
        second = compiler.compile(opportunity, intent, bundle, dossier=dossier, quality_assessment=quality)
        assert first.expansion_id == second.expansion_id
        assert first.definition_hash == second.definition_hash

    asyncio.run(run())


def test_compiler_supports_verified_single_source_continuation() -> None:
    async def run():
        evidence = single_source_fixture()
        ref = evidence.evidence_frontier[0]
        spec = WorkContextSpec.create(planner_version="planner-v1", objective="Continue from the verified state.", separation_reason="The continuation needs a clean independent history.", questions=("What remains after the verified state?",), completion_criteria=("The next bounded step is complete.",), workspace_requirement="read_only", evidence_requirements=(EvidenceRequirement(requirement_id="state", role="conversation", question="What state is verified?", coverage_criterion="The exact source message is available."),))
        bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=(ResolvedEvidenceItem(requirement_id="state", ref=ref, content_hash=evidence.sources[0].content_hash, relevance_reason="Exact verified state."),))
        opportunity = ExpansionOpportunity.create(loop_id="loop-single", round_id="round-single", observation_hash="b" * 64, work_spec=spec, manifest_sources=(evidence.sources[0].source,))
        dossier, quality = await _verified(spec, bundle)
        compiled = DeterministicExpansionPlanCompiler().compile(opportunity, SpawnContextIntent(opportunity_id=opportunity.opportunity_id), bundle, dossier=dossier, quality_assessment=quality)
        assert compiled.plan.source_frontier == (evidence.sources[0].source,)
        assert any(isinstance(item, CopyMessage) for item in compiled.plan.items)

    asyncio.run(run())


def test_compiler_rejects_stale_evidence_source_in_bundle_contract() -> None:
    _, bundle, _ = _inputs()
    stale = bundle.model_copy(update={"source_frontier": (revision_ref("stale"), *bundle.source_frontier[1:])})
    with pytest.raises(ValueError):
        ResolvedEvidenceBundle.model_validate(stale.model_dump(mode="json"))
