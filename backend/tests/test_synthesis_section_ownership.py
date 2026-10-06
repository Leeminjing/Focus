"""本文件对外提供作者输出单一分组归属和稳定声明物化回归。
输入为嵌套分组、跨组premise和非法旧双清单；输出为构造性完整覆盖及精确身份/图拒绝断言。
具体工作流为从生产Worker Schema解析唯一声明集合，物化后交给原确定性validator，再经过生产模型调用核对分组覆盖。
示例：pytest backend/tests/test_synthesis_section_ownership.py -q；可控模型仅检查协议，不替代真实publication或支持判断。
"""

import asyncio

import pytest
from pydantic import ValidationError

from backend.app.desktop.agent_loop.context_expansion.synthesis import ContextSynthesisClaim, ContextSynthesisValidator, ContextSynthesisWorkerDraft
from backend.tests.test_synthesis_claim_feedback import _inputs, _service


def _sections():
    spec, bundle = _inputs()
    claim = {"claim_key": "state", "statement": "Continue from this verified state.", "authority": "confirmed",
        "citations": [bundle.items[0].ref.model_dump(mode="json")], "requirement_ids": ["state"],
        "question_ids": [ContextSynthesisValidator.question_id(spec.questions[0])]}
    inference = {"claim_key": "next", "statement": "The next review can start from the provided state.",
        "authority": "inference", "premise_claim_keys": ["state"]}
    return spec, bundle, [{"title": "Evidence", "claims": [claim]}, {"title": "Next review", "claims": [inference]}]


def test_nested_sections_construct_exact_cover_with_cross_section_premise():
    spec, bundle, sections = _sections()
    draft = ContextSynthesisWorkerDraft.model_validate({"sections": sections}).materialize()
    ContextSynthesisValidator().validate_draft(spec, bundle, draft)
    assert len(draft.claims) == 2
    assert tuple(key for s in draft.sections for key in s.claim_ids) == tuple(c.claim_id for c in draft.claims)
    assert draft.claims[1].premise_claim_ids == (draft.claims[0].claim_id,)
    expected = ContextSynthesisClaim.create(statement=sections[0]["claims"][0]["statement"], authority="confirmed",
        citations=(bundle.items[0].ref,), requirement_ids=("state",), question_ids=(ContextSynthesisValidator.question_id(spec.questions[0]),))
    assert draft.claims[0].claim_id == expected.claim_id


def test_provider_schema_has_one_claim_inventory_and_nonempty_sections():
    schema = ContextSynthesisWorkerDraft.model_json_schema()
    assert "claims" not in schema["properties"]
    section = schema["$defs"]["SynthesisSectionDraft"]
    assert "claim_keys" not in section["properties"]
    assert section["properties"]["claims"]["minItems"] == 1
    assert section["additionalProperties"] is False
    assert schema["properties"]["sections"]["minItems"] == 1


def test_section_display_order_does_not_reject_a_valid_cross_section_dependency():
    spec, bundle, sections = _sections()
    sections.reverse()
    draft = ContextSynthesisWorkerDraft.model_validate({"sections": sections}).materialize()
    ContextSynthesisValidator().validate_draft(spec, bundle, draft)
    assert tuple(s.title for s in draft.sections) == ("Next review", "Evidence")
    assert draft.claims[0].authority == "confirmed"
    assert draft.claims[1].premise_claim_ids == (draft.claims[0].claim_id,)


@pytest.mark.parametrize("invalid", ["duplicate", "cycle", "unknown"])
def test_nested_graph_preserves_global_key_and_premise_rejection(invalid):
    _, _, sections = _sections()
    if invalid == "duplicate":
        sections[1]["claims"][0] = dict(sections[0]["claims"][0])
        rule = "claim_key 重复"
    elif invalid == "cycle":
        sections[0]["claims"][0].update(authority="inference", premise_claim_keys=["next"])
        rule = "premise graph 必须无环"
    else:
        sections[1]["claims"][0]["premise_claim_keys"] = ["missing"]
        rule = "premise 必须引用已知"
    worker = ContextSynthesisWorkerDraft.model_validate({"sections": sections})
    with pytest.raises(ValueError, match=rule):
        worker.materialize()


@pytest.mark.parametrize("invalid", ["empty", "flat", "parallel"])
def test_empty_or_redundant_claim_inventories_are_schema_errors(invalid):
    _, _, sections = _sections()
    payload = {"sections": sections}
    if invalid == "empty":
        sections[0]["claims"] = []
    elif invalid == "flat":
        payload = {"sections": [{"title": "Evidence", "claim_keys": ["state"]}], "claims": [sections[0]["claims"][0]]}
    else:
        payload["claims"] = [sections[0]["claims"][0]]
    with pytest.raises(ValidationError):
        ContextSynthesisWorkerDraft.model_validate(payload)


def test_actual_author_request_and_materialized_dossier_preserve_exact_coverage(monkeypatch):
    async def run():
        service, spec, bundle, _, _, calls = _service(monkeypatch)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is not None, result.blocker_summary
        draft = result.dossier
        assert tuple(key for s in draft.sections for key in s.claim_ids) == tuple(c.claim_id for c in draft.claims)
        assert [role for role, _ in calls] == ["synthesis", "verifier"]
    asyncio.run(run())
