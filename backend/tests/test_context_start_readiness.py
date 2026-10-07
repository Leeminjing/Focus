"""本文件对外提供真实 bootstrap 质量回放与创建前问题处置回归。

输入为脱敏真实材料、授权能力和受控独立评估；输出为原失败保留、身份变化、权限缺口与研究投影断言。
具体工作流先回放冻结评估，再验证问题身份与实际装备边界；受控 verdict 不替代真实模型验收。
示例：python -m pytest backend/tests/test_context_start_readiness.py -q。
"""

import asyncio
import json
from pathlib import Path

import pytest

from backend.app.desktop.agent_loop.context_expansion.contracts import ResolvedEvidenceBundle, WorkContextSpec
from backend.app.desktop.agent_loop.context_expansion.quality import ContextQualityVerifier
from backend.app.desktop.agent_loop.context_expansion.quality_input_scope import quality_input_scope
from backend.app.desktop.agent_loop.context_expansion.start_readiness import ExecutionReadiness, QuestionDisposition, work_question_id
from backend.app.desktop.agent_loop.context_expansion.synthesis import ContextSynthesisDraft, ContextSynthesisValidator, ValidatedContextDossier


def _frozen():
    data = json.loads((Path(__file__).parent / "fixtures/context_quality/bootstrap_failure.json").read_text(encoding="utf-8"))
    return (WorkContextSpec.model_validate(data["work_spec"]),
            ResolvedEvidenceBundle.model_validate(data["evidence_resolution"]),
            ValidatedContextDossier.model_validate(data["dossier_synthesis"]), data["context_quality"])


def test_actual_bootstrap_failed_assessment_replays_without_generation():
    spec, bundle, dossier, assessment = _frozen()
    result = ContextQualityVerifier().restore(spec, bundle, dossier, assessment)
    assert result.preflight.eligible
    assert result.blocker_code == "context_quality_failed"
    assert {d.dimension: d.verdict for d in result.assessment.dimensions} == {
        "minimality": "fail", "sufficiency": "fail", "coherence": "pass"}


def _scope(capabilities=("read", "host_command")):
    return ExecutionReadiness(workspace_id="fixture-workspace", goal_revision=1, authority_revision=1,
                              capabilities=capabilities, workspace_mode="isolated_write")


def _research(spec, *, disposition="execution_research", capability="host_command"):
    return tuple(QuestionDisposition(question_id=work_question_id(q), disposition=disposition,
                                    investigation=f"调查实际技术要求：{q}", required_capabilities=(capability,)) for q in spec.questions)


@pytest.mark.parametrize("invalid", ["identity", "duplicate", "capability"])
def test_platform_rejects_invented_questions_and_unequipped_capabilities(invalid):
    spec, _, _, _ = _frozen()
    plans = _research(spec)
    if invalid == "identity":
        plans = (plans[0].model_copy(update={"question_id": "0" * 64}),)
    elif invalid == "duplicate":
        plans = (plans[0], plans[0])
    else:
        plans = _research(spec, capability="write")
    with pytest.raises(ValueError):
        _scope().validate_plans(plans, spec.questions)


def test_read_only_role_cannot_request_host_investigation():
    spec, _, _, _ = _frozen()
    with pytest.raises(ValueError):
        _scope(("read",)).validate_plans(_research(spec), spec.questions)


def test_feasible_research_changes_dossier_identity_preserves_facts_and_does_not_override_verdict():
    spec, bundle, dossier, _ = _frozen()
    draft = ContextSynthesisDraft(sections=dossier.sections, claims=dossier.claims,
        unresolved_questions=spec.questions, question_dispositions=_research(spec))
    revised = ContextSynthesisValidator().validate(spec, bundle, draft, dossier.support_assessments,
        synthesizer_version="readiness-fixture-v1", execution_readiness=_scope())
    assert revised.dossier_id != dossier.dossier_id
    assert revised.claims == dossier.claims and revised.support_assessments == dossier.support_assessments
    scope = quality_input_scope(spec, bundle, revised)
    assert scope["execution_readiness"]["capabilities"] == ["read", "host_command"]
    assert len(scope["question_dispositions"]) == len(spec.questions)
    assert "auditability" in scope["provenance_purpose"]
    result = ContextQualityVerifier().verify(spec, bundle, revised, {"dimensions": [
        {"dimension": d, "verdict": "unknown", "reasons": ["Independent decision remains unknown"]}
        for d in ("minimality", "sufficiency", "coherence")]})
    assert result.preflight.eligible and result.blocker_code == "context_quality_failed"


def test_unsolved_prerequisite_remains_blocked_before_model():
    spec, bundle, dossier, _ = _frozen()
    updated = dossier.model_copy(update={"question_dispositions": _research(spec, disposition="prerequisite"),
                                        "execution_readiness": _scope()})
    result = ContextQualityVerifier().preflight(spec, bundle, updated)
    assert not result.eligible and "start_readiness_blocked" in result.blocker_codes


def test_mandatory_boundaries_are_retained_in_execution_material():
    from backend.app.desktop.agent_loop.mission_contract import ExecutionBoundaries
    from backend.app.desktop.agent_loop.context_expansion.synthesis import render_context_dossier

    _, _, dossier, _ = _frozen()
    scope = _scope().model_copy(update={"mission_boundaries": ExecutionBoundaries(
        required_invariants=("Keep module boundaries",), prohibited_actions=("Do not use a real Vault",))})
    text = render_context_dossier(dossier.model_copy(update={"execution_readiness": scope}))
    assert "Keep module boundaries" in text and "Do not use a real Vault" in text
