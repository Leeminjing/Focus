"""本文件提供宿主语义资格在最终索引、局部 record 与整体 interpretation 的回归。

输入为有意违反 prompt 的 drafts、精确引用及独立 supported verdict；输出为资格隔离、正常证据接纳和旧 proof 无损读取断言。
工作流为编译真实 handoff，分别走三层验证，并以旧版本 hash 重建历史 payload 验证只读兼容。
示例：pytest backend/tests/test_semantic_eligibility_repairs.py；无网络或用户数据写入。
"""

from copy import deepcopy

import pytest

from backend.tests.test_commitment_handoff import _completed
from backend.tests.test_context_revision_reader import _ref
from backend.app.desktop.context_evolution import ContextRevisionPayloadMode
from backend.app.desktop.agent_loop.context_expansion.contracts import stable_expansion_hash
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import RevisionSemanticIndexer
from backend.app.desktop.agent_loop.context_expansion.semantic_index import RevisionSemanticIndex
from backend.app.desktop.agent_loop.context_expansion.semantic_grounding import (
    SegmentSemanticUnitDraft, SemanticSupportSpan, SemanticClaimSupportAssessment, SemanticGroundingValidator,
)
from backend.app.desktop.agent_loop.context_expansion.segment_projection import SegmentProjectionRecord
from backend.app.desktop.agent_loop.context_expansion.interpretation_record import RevisionInterpretationRecord
from backend.app.desktop.agent_loop.context_expansion.interpretation_inputs import FrozenInterpretationInputs
from focus.agents.commitment.handoff import compile_handoff
from focus.history import content_hash, messages_to_items, semantic_messages


def _inputs(kind="decision", authority="confirmed"):
    state, trigger, checkpoint = _completed()
    delivery = compile_handoff(state, trigger, checkpoint, [])
    rows = semantic_messages(messages_to_items([trigger, *delivery]))
    draft = SegmentSemanticUnitDraft(kind=kind, authority=authority, statement="FROZEN_KNOWLEDGE",
        supports=(SemanticSupportSpan(message_id=delivery[1].id, quote="FROZEN_KNOWLEDGE"),))
    assessment = SemanticClaimSupportAssessment(claim_key=draft.claim_key, verdict="supported", reason="Exact quotation")
    return rows, draft, assessment


def _index(rows, drafts=(), assessments=()):
    return RevisionSemanticIndexer().index(source=_ref("probe", "probe-r", ContextRevisionPayloadMode.CHECKPOINT),
        source_content_hash=content_hash("probe"), context_role="main", active_objective="task", raw_messages=rows,
        unit_drafts=drafts, claim_support_assessments=assessments)


@pytest.mark.parametrize("kind", ["decision", "claim", "hypothesis", "unresolved_question"])
@pytest.mark.parametrize("authority", ["confirmed", "hypothesis"])
def test_reference_only_cannot_independently_define_task_units(kind, authority):
    rows, draft, assessment = _inputs(kind, authority)
    index = _index(rows, (draft,), (assessment,) if authority == "confirmed" else ())
    assert len(index.rejected_units) == 1 and index.rejected_units[0].code == "semantic_eligibility_error"
    assert all(unit.statement != draft.statement for unit in index.semantic_units)


def test_local_and_interpretation_share_eligibility_rejection():
    rows, draft, assessment = _inputs()
    index = _index(rows)
    local = SegmentProjectionRecord.create(context_id="probe", contract="current-local", segment=index.segments[0],
        messages=index.messages, drafts=(draft,), assessments=(assessment,))
    assert local.accepted_claim_keys == () and local.rejections[0].code == "semantic_eligibility_error"
    inputs = FrozenInterpretationInputs(index, (local,), max_reads=1)
    inputs.read((index.segments[0].segment_id,))
    whole = RevisionInterpretationRecord.create(context_id="probe", source_content_hash=index.source_content_hash,
        contract_fingerprint="current-whole", local_contract_fingerprint="current-local", inventory=inputs.inventory,
        provided=inputs.provided, drafts=(draft,), assessments=(assessment,))
    assert whole.accepted_claim_keys == () and whole.rejections[0].code == "semantic_eligibility_error"
    assert whole.accepted_units(index.source) == ()
    assert whole.grounding_version == local.grounding_version == SemanticGroundingValidator.VERSION
    assert SegmentProjectionRecord.model_validate(local.model_dump(mode="json")) == local
    assert RevisionInterpretationRecord.model_validate(whole.model_dump(mode="json")) == whole


@pytest.mark.parametrize("kind,accepted", [("decision", False), ("unresolved_question", False),
    ("claim", True), ("verification_result", True), ("implementation_effect", True), ("failure", True)])
def test_evidence_only_preserves_evidence_results_without_defining_task_scope(kind, accepted):
    rows = ({"id": "evidence", "role": "human", "content": "Teammate observed a failed test.", "semantic_policy": "evidence_only"},)
    draft = SegmentSemanticUnitDraft(kind=kind, authority="confirmed", statement="Teammate observed a failed test.",
        supports=(SemanticSupportSpan(message_id="evidence", quote="Teammate observed a failed test."),))
    assessment = SemanticClaimSupportAssessment(claim_key=draft.claim_key, verdict="supported", reason="Exact quotation")
    index = _index(rows, (draft,), (assessment,))
    assert bool(index.projected_unit_ids) is accepted
    assert bool(index.rejected_units) is not accepted
    assert index.fallback_segment_ids == ()


def test_index_with_reference_support_can_define_a_verified_task_decision():
    rows, draft, _ = _inputs()
    draft = draft.model_copy(update={"supports": (*draft.supports, SemanticSupportSpan(message_id="input", quote="做X"))})
    assessment = SemanticClaimSupportAssessment(claim_key=draft.claim_key, verdict="supported", reason="Joint quoted decision")
    index = _index(rows, (draft,), (assessment,))
    assert index.rejected_units == () and any(unit.kind == "decision" for unit in index.semantic_units)


def test_old_local_and_whole_proofs_preserve_original_identity_and_algorithm():
    rows, draft, assessment = _inputs()
    index = _index(rows)
    current = SegmentProjectionRecord.create(context_id="probe", contract="old-local", segment=index.segments[0],
        messages=index.messages, drafts=(draft,), assessments=(assessment,))
    payload = current.model_dump(mode="json")
    payload.pop("grounding_version")
    accepted, rejected, fallback = SegmentProjectionRecord._dispositions(index.messages, (draft,), (assessment,), "support-span-grounding-v1")
    payload.update(accepted_claim_keys=list(accepted), rejections=[r.model_dump(mode="json") for r in rejected], fallback=fallback)
    payload["record_id"] = stable_expansion_hash("segment-projection-record-v1", {k: v for k, v in payload.items() if k != "record_id"})
    original = deepcopy(payload)
    local = SegmentProjectionRecord.model_validate(payload)
    assert local.model_dump(mode="json") == original and local.accepted_claim_keys == (draft.claim_key,)
    inputs = FrozenInterpretationInputs(index, (local,), max_reads=1)
    inputs.read((index.segments[0].segment_id,))
    whole = RevisionInterpretationRecord.create(context_id="probe", source_content_hash=index.source_content_hash,
        contract_fingerprint="old-whole", local_contract_fingerprint="old-local", inventory=inputs.inventory,
        provided=inputs.provided, drafts=(draft,), assessments=(assessment,))
    payload = whole.model_dump(mode="json")
    payload.pop("grounding_version")
    payload.update(accepted_claim_keys=[draft.claim_key], rejections=[])
    payload["record_id"] = stable_expansion_hash("revision-interpretation-record-v1", {k: v for k, v in payload.items() if k != "record_id"})
    original = deepcopy(payload)
    restored = RevisionInterpretationRecord.model_validate(payload)
    assert restored.model_dump(mode="json") == original
    assert len(restored.accepted_units(index.source)) == 1


def test_v7_index_is_read_only_compatible_but_cannot_bypass_v8_eligibility():
    rows, draft, assessment = _inputs()
    index = _index(rows)
    legacy_unit = SemanticGroundingValidator().validate(index.source,
        RevisionSemanticIndexer.message_contents(index.messages), draft, assessment)
    common = dict(source=index.source, source_content_hash=index.source_content_hash, context_role=index.context_role,
        active_objective=index.active_objective, messages=index.messages, segments=index.segments,
        semantic_units=(*index.semantic_units, legacy_unit), projected_unit_ids=(legacy_unit.unit_id,),
        fallback_segment_ids=index.fallback_segment_ids, quality_state="degraded",
        segmenter_version=index.segmenter_version, projector_version=index.projector_version)
    old = RevisionSemanticIndex.create(**common, index_schema_version="revision-semantic-index-v7")
    payload = deepcopy(old.model_dump(mode="json"))
    assert RevisionSemanticIndex.model_validate(payload).model_dump(mode="json") == payload
    with pytest.raises(ValueError, match="独立定义"):
        RevisionSemanticIndex.create(**common, index_schema_version=RevisionSemanticIndexer.INDEX_SCHEMA_VERSION)
    assert old.model_dump(mode="json") == payload and old.index_id != index.index_id
