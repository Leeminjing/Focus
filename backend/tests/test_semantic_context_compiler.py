r"""本文件对外提供 evidence-grounded dossier 与 deterministic semantic context compiler 的纯测试。

输入为冻结 R3/R8/R5/F2 bundle、合法或越界 dossier 与 identity-only Patrol intent；输出为固定四层 plan、稳定 definition hash、
原始证据权威和 synthesis omission 断言。具体工作流为直接调用 validator 与纯 compiler，不连接数据库或发布 Portfolio。
示例：`pytest backend/tests/test_semantic_context_compiler.py -q`。
"""

from __future__ import annotations

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
from backend.app.desktop.agent_loop.context_expansion.dossier import (
    DossierDraft,
    DossierStatement,
    DossierValidator,
)
from backend.app.desktop.context_curation import (
    ComposeMessage,
    CopyMessage,
    ToolExchange,
)
from backend.tests._semantic_context_fixtures import (
    failure_analysis_fixture,
    revision_ref,
    single_source_fixture,
)


def _inputs():
    spec, evidence, items = failure_analysis_fixture()
    opportunity = ExpansionOpportunity.create(
        loop_id="loop-semantic",
        round_id="round-semantic",
        observation_hash="a" * 64,
        work_spec=spec,
        manifest_sources=tuple(source.source for source in evidence.sources),
    )
    bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=items)
    intent = SpawnContextIntent(opportunity_id=opportunity.opportunity_id)
    return opportunity, bundle, intent


def test_compiler_orders_contract_ledger_dossier_before_primary_evidence() -> None:
    opportunity, bundle, intent = _inputs()
    dossier = DossierValidator().validate(
        bundle,
        DossierDraft(
            statements=(
                DossierStatement(
                    statement="The observed failure conflicts with the required lock behavior.",
                    citations=(bundle.items[0].ref, bundle.items[-1].ref),
                ),
            )
        ),
    )
    compiled = DeterministicExpansionPlanCompiler().compile(
        opportunity,
        intent,
        bundle,
        dossier=dossier,
    )

    assert not hasattr(compiled, "code")
    assert isinstance(compiled.plan.items[0], ComposeMessage)
    assert compiled.plan.items[0].content.startswith("Work Contract")
    assert compiled.plan.items[1].content.startswith("Evidence Ledger")
    assert compiled.plan.items[2].content.startswith("Evidence-grounded Dossier")
    assert any(isinstance(item, CopyMessage) for item in compiled.plan.items[3:])
    assert any(isinstance(item, ToolExchange) for item in compiled.plan.items[3:])
    assert compiled.plan.source_frontier == bundle.source_frontier
    assert compiled.plan.evidence_frontier == bundle.evidence_frontier


def test_raw_primary_evidence_remains_present_when_dossier_claim_conflicts() -> None:
    opportunity, bundle, intent = _inputs()
    dossier = DossierValidator().validate(
        bundle,
        DossierDraft(
            statements=(
                DossierStatement(
                    statement="The account was locked successfully.",
                    citations=(bundle.items[-1].ref,),
                ),
            )
        ),
    )
    compiled = DeterministicExpansionPlanCompiler().compile(opportunity, intent, bundle, dossier=dossier)

    assert "locked successfully" in compiled.plan.items[2].content
    assert dossier.statements[0].authority == "hypothesis"
    structured = [item for item in compiled.plan.items if isinstance(item, ComposeMessage)][-2:]
    assert any("account remained unlocked" in item.content for item in structured)


def test_unknown_dossier_citation_is_rejected() -> None:
    _, bundle, _ = _inputs()
    mission_ref = next(item.ref for item in bundle.items if getattr(item.ref, "kind", None) == "mission")
    unknown = mission_ref.model_copy(update={"item_id": "unknown"})

    with pytest.raises(ValueError, match="resolved bundle"):
        DossierValidator().validate(
            bundle,
            DossierDraft(statements=(DossierStatement(statement="Unknown.", citations=(unknown,)),)),
        )


def test_synthesis_omission_keeps_contract_ledger_and_primary_evidence() -> None:
    opportunity, bundle, intent = _inputs()
    compiled = DeterministicExpansionPlanCompiler().compile(
        opportunity,
        intent,
        bundle,
        synthesis_omitted=True,
    )

    assert compiled.synthesis_omitted is True
    assert compiled.dossier_id is None
    assert compiled.plan.items[0].content.startswith("Work Contract")
    assert compiled.plan.items[1].content.startswith("Evidence Ledger")
    assert not any("Evidence-grounded Dossier" in getattr(item, "content", "") for item in compiled.plan.items)


def test_compiler_definition_is_stable_for_identical_frozen_inputs() -> None:
    opportunity, bundle, intent = _inputs()
    compiler = DeterministicExpansionPlanCompiler()

    first = compiler.compile(opportunity, intent, bundle, synthesis_omitted=True)
    second = compiler.compile(opportunity, intent, bundle, synthesis_omitted=True)

    assert first.expansion_id == second.expansion_id
    assert first.definition_hash == second.definition_hash


def test_compiler_supports_exact_single_source_continuation() -> None:
    evidence = single_source_fixture()
    ref = evidence.evidence_frontier[0]
    spec = WorkContextSpec.create(
        planner_version="planner-v1",
        objective="Continue from the verified state.",
        separation_reason="The continuation needs a clean independent history.",
        questions=("What remains after the verified state?",),
        completion_criteria=("The next bounded step is complete.",),
        workspace_requirement="read_only",
        evidence_requirements=(
            EvidenceRequirement(
                requirement_id="state",
                role="conversation",
                question="What state is verified?",
                coverage_criterion="The exact source message is available.",
            ),
        ),
    )
    bundle = ResolvedEvidenceBundle.create(
        work_spec=spec,
        evidence=evidence,
        items=(
            ResolvedEvidenceItem(
                requirement_id="state",
                ref=ref,
                content_hash=evidence.sources[0].content_hash,
                relevance_reason="Exact verified state.",
            ),
        ),
    )
    opportunity = ExpansionOpportunity.create(
        loop_id="loop-single",
        round_id="round-single",
        observation_hash="b" * 64,
        work_spec=spec,
        manifest_sources=(evidence.sources[0].source,),
    )
    compiled = DeterministicExpansionPlanCompiler().compile(
        opportunity,
        SpawnContextIntent(opportunity_id=opportunity.opportunity_id),
        bundle,
    )

    assert compiled.plan.source_frontier == (evidence.sources[0].source,)
    assert any(isinstance(item, CopyMessage) for item in compiled.plan.items)


def test_compiler_rejects_stale_evidence_source_in_bundle_contract() -> None:
    _, bundle, _ = _inputs()
    stale = bundle.model_copy(
        update={"source_frontier": (revision_ref("stale"), *bundle.source_frontier[1:])}
    )

    with pytest.raises(ValueError):
        ResolvedEvidenceBundle.model_validate(stale.model_dump(mode="json"))
