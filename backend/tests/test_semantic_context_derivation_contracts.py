r"""本文件对外提供 semantic context derivation 的 authority、typed evidence 与 work-spec 合同测试。

输入为冻结 R3/R8/R5/F2 fixture、非法 evidence payload 与等价工作规格；输出为 identity、coverage、frontier、
协议边界和模块依赖断言。具体工作流为调用公开合同与通用 compiler，不连接数据库或产生 Portfolio 写入。
示例：`pytest backend/tests/test_semantic_context_derivation_contracts.py -q`。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    EvidenceRequirement,
    ExpansionOpportunity,
    ResolvedEvidenceBundle,
    SemanticEvidenceUnit,
    SpawnContextIntent,
    WorkContextSpec,
)
from backend.app.desktop.context_curation import (
    ComposeMessage,
    CreateLanePlan,
    LaneCompilationError,
    MultiSourceEvidence,
    ToolExchange,
    ToolExchangeCall,
    compile_lane,
)
from backend.tests._semantic_context_fixtures import (
    duplicate_message_id_fixture,
    failure_analysis_fixture,
    incomplete_tool_exchange_fixture,
    revision_ref,
    single_source_fixture,
)

ROOT = Path(__file__).parents[2]


def test_patrol_spawn_intent_remains_identity_only() -> None:
    intent = SpawnContextIntent(opportunity_id="a" * 64)

    assert set(intent.model_dump()) == {"action", "opportunity_id"}


def test_context_expansion_modules_do_not_import_commit_authority() -> None:
    package = ROOT / "backend" / "app" / "desktop" / "agent_loop" / "context_expansion"
    forbidden = {
        "backend.app.desktop.agent_loop.kernel",
        "backend.app.desktop.context_curation.portfolio_publisher",
    }
    violations: list[str] = []
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        if imports & forbidden:
            violations.append(path.name)
    assert not violations


def test_work_spec_identity_is_stable_across_equivalent_ordering() -> None:
    requirements = (
        EvidenceRequirement(
            requirement_id="test",
            role="test",
            question="Which test covers it?",
            coverage_criterion="An exact assertion is present.",
        ),
        EvidenceRequirement(
            requirement_id="requirement",
            role="requirement",
            question="What is required?",
            coverage_criterion="The exact requirement is present.",
        ),
    )
    left = WorkContextSpec.create(
        planner_version="planner-v1",
        objective=" Diagnose the failure ",
        separation_reason="Use independent evidence",
        questions=("What failed?", "Why?"),
        completion_criteria=("Reproduce it", "Explain it"),
        workspace_requirement="read_only",
        evidence_requirements=requirements,
    )
    right = WorkContextSpec.create(
        planner_version="planner-v1",
        objective="Diagnose   the failure",
        separation_reason="Use independent evidence",
        questions=("Why?", "What failed?"),
        completion_criteria=("Explain it", "Reproduce it"),
        workspace_requirement="read_only",
        evidence_requirements=tuple(reversed(requirements)),
    )

    assert left == right


def test_work_spec_identity_changes_with_material_question() -> None:
    spec, _, _ = failure_analysis_fixture()
    changed = WorkContextSpec.create(
        planner_version=spec.planner_version,
        objective=spec.objective,
        separation_reason=spec.separation_reason,
        questions=("Is the database write ordered incorrectly?",),
        completion_criteria=spec.completion_criteria,
        workspace_requirement=spec.workspace_requirement,
        evidence_requirements=spec.evidence_requirements,
    )

    assert changed.work_spec_id != spec.work_spec_id


def test_work_spec_identity_changes_with_evidence_role() -> None:
    spec, _, _ = failure_analysis_fixture()
    changed_requirement = spec.evidence_requirements[0].model_copy(update={"role": "decision"})
    changed = WorkContextSpec.create(
        planner_version=spec.planner_version,
        objective=spec.objective,
        separation_reason=spec.separation_reason,
        questions=spec.questions,
        completion_criteria=spec.completion_criteria,
        workspace_requirement=spec.workspace_requirement,
        evidence_requirements=(changed_requirement, *spec.evidence_requirements[1:]),
    )

    assert changed.work_spec_id != spec.work_spec_id


def test_opportunity_identity_uses_frozen_observation_and_work_spec_not_manifest_order() -> None:
    spec, evidence, _ = failure_analysis_fixture()
    sources = tuple(item.source for item in evidence.sources)
    left = ExpansionOpportunity.create(
        loop_id="loop-semantic",
        round_id="round-7",
        observation_hash="a" * 64,
        work_spec=spec,
        manifest_sources=sources,
        manifest_ids=("manifest-b", "manifest-a"),
        signal_ids=("signal-b", "signal-a"),
    )
    right = ExpansionOpportunity.create(
        loop_id="loop-semantic",
        round_id="round-7",
        observation_hash="a" * 64,
        work_spec=spec,
        manifest_sources=tuple(reversed(sources)),
        manifest_ids=("manifest-a", "manifest-b"),
        signal_ids=("signal-a", "signal-b"),
    )

    assert left == right
    assert left.independence_key == spec.work_spec_id


def test_confirmed_semantic_unit_requires_exact_evidence() -> None:
    with pytest.raises(ValidationError):
        SemanticEvidenceUnit.create(
            kind="claim",
            authority="confirmed",
            statement="The implementation is correct.",
        )

    hypothesis = SemanticEvidenceUnit.create(
        kind="hypothesis",
        authority="hypothesis",
        statement="The transaction may be ordered incorrectly.",
    )
    assert hypothesis.evidence_refs == ()


def test_multi_source_bundle_covers_r3_r8_r5_f2_without_lexical_lookup() -> None:
    spec, evidence, items = failure_analysis_fixture()
    bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=items)

    assert {item.requirement_id for item in bundle.items} == {"R3", "R8", "R5", "F2"}
    assert len(bundle.evidence_frontier) == 5
    assert len(bundle.source_frontier) == 2


def test_resolution_identity_is_stable_across_source_frontier_and_item_order() -> None:
    spec, evidence, items = failure_analysis_fixture()
    left = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=items)
    reordered = evidence.model_copy(
        update={
            "sources": tuple(reversed(evidence.sources)),
            "evidence_frontier": tuple(reversed(evidence.evidence_frontier)),
        }
    )
    right = ResolvedEvidenceBundle.create(
        work_spec=spec,
        evidence=reordered,
        items=tuple(reversed(items)),
    )

    assert left.resolution_id == right.resolution_id
    assert left.source_frontier == right.source_frontier


def test_required_coverage_rejects_missing_failure_evidence() -> None:
    spec, evidence, items = failure_analysis_fixture()

    with pytest.raises(ValidationError, match="required evidence requirement"):
        ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=items[:-1])


def test_structured_evidence_requires_explicit_frontier() -> None:
    _, evidence, _ = failure_analysis_fixture()
    payload = evidence.model_dump(mode="json")
    payload["evidence_frontier"] = []

    with pytest.raises(ValidationError, match="必须声明 evidence frontier"):
        MultiSourceEvidence.model_validate(payload)


def test_unknown_evidence_kind_is_rejected() -> None:
    source = single_source_fixture().sources[0].source

    with pytest.raises(ValidationError):
        ComposeMessage.model_validate(
            {
                "type": "compose_message",
                "role": "human",
                "content": "Unsupported evidence.",
                "sources": [
                    {
                        "kind": "unknown",
                        "loop_id": "loop",
                        "goal_revision": 1,
                        "item_id": "x",
                        "content_hash": "a" * 64,
                    }
                ],
            }
        )
    assert source.is_runnable


def test_generic_compiler_preserves_typed_lineage() -> None:
    _, evidence, _ = failure_analysis_fixture()
    plan = CreateLanePlan(
        action="create",
        purpose="Failure analysis",
        source_frontier=tuple(source.source for source in evidence.sources),
        evidence_frontier=evidence.evidence_frontier,
        items=(
            ComposeMessage(
                type="compose_message",
                role="human",
                content="Analyze R3, R8, R5, and F2 together.",
                sources=evidence.evidence_frontier,
            ),
        ),
    )
    compiled = compile_lane(plan, evidence)

    assert compiled.evidence_frontier == evidence.evidence_frontier
    assert len(compiled.message_lineage[0].sources) == 5
    assert sum(item.action == "used" for item in compiled.source_dispositions) == 5


def test_duplicate_message_ids_remain_namespaced() -> None:
    evidence = duplicate_message_id_fixture()

    keys = [message.ref.key for source in evidence.sources for message in source.messages]
    assert len(keys) == len(set(keys)) == 2
    assert {key[-1] for key in keys} == {"shared"}


def test_incomplete_tool_exchange_is_rejected_before_compilation() -> None:
    evidence = incomplete_tool_exchange_fixture()
    source = evidence.sources[0]
    caller = source.messages[0].ref
    plan = CreateLanePlan(
        action="create",
        purpose="Incomplete exchange",
        source_frontier=(source.source,),
        evidence_frontier=(caller,),
        items=(
            ToolExchange(
                type="tool_exchange",
                assistant_content="Read the file.",
                calls=(
                    ToolExchangeCall(
                        name="read_file",
                        args={"path": "a.py"},
                        result_content="missing",
                    ),
                ),
                sources=(caller,),
            ),
        ),
    )

    with pytest.raises(LaneCompilationError, match="ToolMessage"):
        compile_lane(plan, evidence)


def test_stale_revision_fixture_has_distinct_exact_identity() -> None:
    frozen = revision_ref("stale", 1)
    latest = revision_ref("stale", 2)

    assert frozen.context_id == latest.context_id
    assert frozen.revision_id != latest.revision_id
    assert frozen.checkpoint_id != latest.checkpoint_id


def test_compiler_rejects_missing_declared_structured_evidence() -> None:
    _, evidence, _ = failure_analysis_fixture()
    missing = evidence.model_copy(update={"structured": evidence.structured[:1]})
    plan = CreateLanePlan(
        action="create",
        purpose="Failure analysis",
        source_frontier=tuple(source.source for source in evidence.sources),
        evidence_frontier=evidence.evidence_frontier,
        items=(
            ComposeMessage(
                type="compose_message",
                role="human",
                content="Analyze all evidence.",
                sources=evidence.evidence_frontier,
            ),
        ),
    )

    with pytest.raises((LaneCompilationError, ValidationError)):
        compile_lane(plan, missing)
