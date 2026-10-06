"""本文件对外提供创建前质量评估阶段与原子协议用途的回归。
输入为冻结WorkSpec、真实Resolver工具闭包、Dossier和固定远端评估；输出为请求阶段、协议成员、原文完整性与原判定保持断言。
具体工作流使用生产RoleBoundStructuredModel捕获唯一提交，校验创建前输入足以执行工作而非已完成工作；额外证据仍阻断。
示例：pytest backend/tests/test_quality_input_scope.py -q；远端fail/unknown原样保存，不模拟真实验收通过。
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from backend.app.desktop.agent_loop.context_expansion.contracts import ResolvedEvidenceBundle, WorkContextSpec
from backend.app.desktop.agent_loop.context_expansion.quality_verifier import StructuredContextQualityService
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import DeterministicTestContextSynthesisService
from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
from backend.app.desktop.context_curation import evidence_ref_key
from backend.tests.config_helpers import app_config_for
from backend.tests.test_tool_exchange_quality_closure import _inputs


@pytest.mark.parametrize("verdict", ["pass", "fail", "unknown"])
@pytest.mark.parametrize("multiple", [False, True])
def test_outbound_review_binds_input_phase_and_atomic_protocol_without_changing_verdict(monkeypatch, verdict, multiple):
    spec, _, bundle, dossier = _inputs(multiple=multiple)
    frozen = [item.model_dump(mode="json") for item in (spec, bundle, dossier)]
    calls = []
    systems = []
    config = app_config_for("quality-scope", None)
    config.models[0].curation_output_method = "prompt_json"

    class Provider:
        async def ainvoke(self, messages, config):
            systems.append(messages[0].content)
            calls.append(json.loads(messages[-1].content.split("<worker_input>", 1)[1].split("</worker_input>", 1)[0]))
            return AIMessage(content=json.dumps({"dimensions": [
                {"dimension": name, "verdict": verdict, "reasons": ["Fixed independent semantic outcome"]}
                for name in ("minimality", "sufficiency", "coherence")]}))

    monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", lambda **kwargs: Provider())
    service = StructuredContextQualityService(RoleBoundStructuredModel(config, "context_quality_verifier"))
    result = asyncio.run(service.verify(spec, bundle, dossier))
    assert len(calls) == 1
    assert result.assessment is not None and {item.verdict for item in result.assessment.dimensions} == {verdict}
    assert result.blocker_code == (None if verdict == "pass" else "context_quality_failed")
    assert [item.model_dump(mode="json") for item in (spec, bundle, dossier)] == frozen
    assert [calls[0][name] for name in ("work_spec", "resolved_evidence", "dossier")] == frozen
    scope = calls[0]["evaluation_scope"]
    assert scope["phase"] == "before_compilation_publication_execution"
    assert scope["evaluation_target"] == "input_adequacy_for_work_spec"
    assert scope["completion_criteria_phase"] == "after_execution"
    assert scope["destination_identity_phase"] == "publication"
    assert (scope["work_spec_id"], scope["resolution_id"], scope["dossier_id"]) == (spec.work_spec_id, bundle.resolution_id, dossier.dossier_id)
    selected = {evidence_ref_key(item.ref) for item in bundle.items}
    cited = {evidence_ref_key(ref) for claim in dossier.claims for ref in claim.citations}
    assert {tuple(key) for key in scope["requirement_evidence_keys"]} == selected
    assert {tuple(key) for key in scope["claim_evidence_keys"]} == cited
    assert {tuple(key) for key in scope["protocol_only_evidence_keys"]} == {evidence_ref_key(ref) for ref in bundle.evidence_frontier} - selected - cited
    assert any(key[-1] == "result-b" for key in scope["protocol_only_evidence_keys"])
    assert {item["revision_id"] for item in scope["existing_source_identities"]} == {item.revision_id for item in bundle.source_frontier}
    assert "尚未创建" in systems[0] and "protocol_only_evidence_keys" in systems[0]


def test_future_destination_completion_criteria_remain_frozen_and_are_not_claimed_satisfied(monkeypatch):
    spec, evidence, original, _ = _inputs()
    spec = WorkContextSpec.create(planner_version=spec.planner_version, objective=spec.objective,
        separation_reason=spec.separation_reason, questions=spec.questions,
        completion_criteria=("After executing the review, return the new Context identity and its actual findings.",),
        workspace_requirement=spec.workspace_requirement, evidence_requirements=spec.evidence_requirements)
    bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=original.evidence, items=original.items)
    dossier = asyncio.run(DeterministicTestContextSynthesisService().synthesize(None, spec, bundle)).dossier
    calls = []

    class Model:
        async def invoke(self, schema, system, payload):
            calls.append(payload)
            return schema.model_validate({"dimensions": [
                {"dimension": name, "verdict": "unknown", "reasons": ["Unresolved historical source conflict"]}
                for name in ("minimality", "sufficiency", "coherence")]})

    result = asyncio.run(StructuredContextQualityService(Model()).verify(spec, bundle, dossier))
    assert len(calls) == 1
    assert calls[0]["work_spec"]["completion_criteria"] == list(spec.completion_criteria)
    scope = calls[0]["evaluation_scope"]
    assert scope["destination_identity_phase"] == "publication" and "destination_identity" not in scope
    assert result.blocker_code == "context_quality_failed"
    assert all(item.verdict == "unknown" for item in result.assessment.dimensions)


def test_scope_does_not_admit_genuinely_unrelated_evidence():
    spec, evidence, original, _ = _inputs()
    bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=original.items)
    dossier = asyncio.run(DeterministicTestContextSynthesisService().synthesize(None, spec, bundle)).dossier

    class Model:
        async def invoke(self, *args):
            pytest.fail("Unrelated evidence must fail preflight before any model submission")

    result = asyncio.run(StructuredContextQualityService(Model()).verify(spec, bundle, dossier))
    assert result.blocker_code == "quality_preflight_failed"
    assert result.preflight.blocker_codes == ("evidence_without_purpose",)
