"""本文件对外提供冻结声明支持拒绝的安全诊断回归。
输入为真实ContextSynthesisValidator、生产RoleBound模型入口与合成冻结证据；输出为精确判定、稳定身份及无秘密正文的断言。
具体工作流为同时保留unsupported、unknown和citation不匹配，核对统一校验的完整计数与首条稳定身份，再检查生产blocker保留诊断及消费。
示例：pytest backend/tests/test_synthesis_support_diagnostics.py -q；不会改写verdict、重试语义拒绝或启动真实Provider。
"""

import asyncio
import json

import pytest

from backend.app.desktop.agent_loop.context_expansion.synthesis import (
    ClaimSupportAssessment,
    ContextSynthesisClaim,
    ContextSynthesisDraft,
    ContextSynthesisValidator,
    SynthesisSection,
)
from backend.app.desktop.context_curation import evidence_ref_key
from backend.tests.test_synthesis_claim_feedback import _inputs, _service


@pytest.mark.parametrize("verdict,citation_match", [("unsupported", True), ("unknown", True), ("supported", False)])
def test_support_rejection_identifies_verdict_without_source_or_reason(verdict, citation_match):
    spec, bundle = _inputs()
    secret = "SYNTHETIC-PRIVATE-DIAGNOSTIC-MARKER"
    claim = ContextSynthesisClaim.create(
        statement=secret, authority="confirmed", citations=bundle.evidence_frontier,
        requirement_ids=("state",), question_ids=(ContextSynthesisValidator.question_id(spec.questions[0]),),
    )
    draft = ContextSynthesisDraft(sections=(SynthesisSection(title="State", claim_ids=(claim.claim_id,)),), claims=(claim,))
    assessment = ClaimSupportAssessment(
        claim_id=claim.claim_id, verdict=verdict, reason=secret,
        citation_keys=tuple(evidence_ref_key(ref) for ref in claim.citations) if citation_match else (),
    )
    with pytest.raises(ValueError, match="direct-support") as caught:
        ContextSynthesisValidator().validate(spec, bundle, draft, (assessment,), synthesizer_version="diagnostic-test-v1")
    message = str(caught.value)
    assert secret not in message
    diagnostic = json.loads(message.split("direct-support 验证: ", 1)[1])
    assert diagnostic == {
        "rejected_count": 1, "unsupported_count": int(verdict == "unsupported"),
        "unknown_count": int(verdict == "unknown"), "citation_mismatch_count": int(not citation_match),
        "first_claim_id": claim.claim_id, "first_verdict": verdict, "first_citation_match": citation_match,
    }


def test_mixed_support_rejection_is_order_independent_and_counts_all_failures():
    spec, bundle = _inputs()
    claims = tuple(ContextSynthesisClaim.create(
        statement=f"Frozen state {index}", authority="confirmed", citations=bundle.evidence_frontier,
        requirement_ids=("state",), question_ids=(ContextSynthesisValidator.question_id(spec.questions[0]),),
    ) for index in range(3))
    ordered = tuple(sorted(claims, key=lambda claim: claim.claim_id))
    assessments = tuple(ClaimSupportAssessment(
        claim_id=claim.claim_id, verdict=verdict, reason="Independent frozen-source assessment",
        citation_keys=tuple(evidence_ref_key(ref) for ref in claim.citations) if index != 2 else (),
    ) for index, (claim, verdict) in enumerate(zip(ordered, ("unknown", "unsupported", "supported"))))
    messages = []
    for items, support in ((claims, assessments), (tuple(reversed(claims)), tuple(reversed(assessments)))):
        draft = ContextSynthesisDraft(sections=(SynthesisSection(title="State", claim_ids=tuple(claim.claim_id for claim in items)),), claims=items)
        with pytest.raises(ValueError, match="direct-support") as caught:
            ContextSynthesisValidator().validate(spec, bundle, draft, support, synthesizer_version="diagnostic-test-v1")
        messages.append(str(caught.value))
    assert messages[0] == messages[1]
    diagnostic = json.loads(messages[0].split("direct-support 验证: ", 1)[1])
    assert diagnostic["rejected_count"] == 3
    assert diagnostic["unsupported_count"] == diagnostic["unknown_count"] == diagnostic["citation_mismatch_count"] == 1
    assert diagnostic["first_claim_id"] == ordered[0].claim_id and diagnostic["first_verdict"] == "unknown"


def test_production_synthesis_preserves_exact_rejection_without_retry(monkeypatch):
    async def run():
        service, spec, bundle, synthesis, verifier, calls = _service(monkeypatch, verifier_error="unsupported")
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is None and result.blocker_code == "synthesis_invalid"
        diagnostic = json.loads(result.blocker_summary.split("direct-support 验证: ", 1)[1])
        assert diagnostic["unsupported_count"] == diagnostic["rejected_count"] == 1
        assert diagnostic["unknown_count"] == 0 and diagnostic["first_citation_match"] is True
        assert [role for role, _ in calls] == ["synthesis", "verifier"]
        assert synthesis.last_usage.model_calls == verifier.last_usage.model_calls == 1
        assert len(result.attempt_records) == 2 and all(record["outcome"] == "success" for record in result.attempt_records)
    asyncio.run(run())
