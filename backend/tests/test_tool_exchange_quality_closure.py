"""本文件对外提供Resolver与质量预检共享Tool Exchange原子性的回归。
输入为多调用Assistant、单个选中结果、重复跨来源调用身份或真正多余证据；输出为完整协议闭包与严格质量阻断断言。
具体工作流为从冻结Corpus解析真实bundle，再对只引用选中证据的合法dossier运行生产preflight；不调用Provider或数据库。
示例：pytest backend/tests/test_tool_exchange_quality_closure.py -q；配对兄弟结果可用，独立多余消息仍拒绝。
"""

import asyncio

import pytest

from backend.app.desktop.agent_loop.context_expansion.contracts import ExpansionBlocker, ResolvedEvidenceBundle
from backend.app.desktop.agent_loop.context_expansion.evidence_resolver import MultiSourceEvidenceResolver
from backend.app.desktop.agent_loop.context_expansion.quality import ContextQualityVerifier
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import DeterministicTestContextSynthesisService
from backend.app.desktop.context_curation import MultiSourceEvidence, SourceMessageEvidence, SourceRevisionEvidence, evidence_ref_key
from backend.tests._semantic_context_fixtures import message_ref, revision_ref
from backend.tests.test_semantic_evidence_resolver import _corpus, _opportunity, _single_requirement_spec, _unit_id


def _source(name):
    revision = revision_ref(name)
    messages = (
        SourceMessageEvidence(ref=message_ref(revision, "caller"), role="ai", content="Read two source files.",
            tool_calls=({"id": "call-a", "name": "read_file", "args": {"path": "a.py"}},
                        {"id": "call-b", "name": "read_file", "args": {"path": "b.py"}})),
        SourceMessageEvidence(ref=message_ref(revision, "result-a"), role="tool", content="Exact source A.", tool_call_id="call-a"),
        SourceMessageEvidence(ref=message_ref(revision, "result-b"), role="tool", content="Exact source B.", tool_call_id="call-b"),
        SourceMessageEvidence(ref=message_ref(revision, "extra"), role="human", content="Unrelated evidence."),
    )
    return SourceRevisionEvidence(source=revision, projection_hash="a" * 64, content_hash="b" * 64, messages=messages)


def _inputs(selected="result-a", *, multiple=False):
    sources = (_source("left"), _source("right")) if multiple else (_source("left"),)
    ref = next(message.ref for message in sources[-1].messages if message.ref.message_id == selected)
    evidence = MultiSourceEvidence(sources=sources, evidence_frontier=tuple(message.ref for source in sources for message in source.messages))
    spec = _single_requirement_spec(role="conversation", candidate_unit_ids=(_unit_id(ref),))
    bundle = MultiSourceEvidenceResolver().resolve(_opportunity(spec, evidence), (), _corpus(evidence, {}))
    assert isinstance(bundle, ResolvedEvidenceBundle)
    dossier = asyncio.run(DeterministicTestContextSynthesisService().synthesize(None, spec, bundle)).dossier
    assert dossier is not None
    return spec, evidence, bundle, dossier


@pytest.mark.parametrize("selected", ["caller", "result-a", "result-b"])
@pytest.mark.parametrize("multiple", [False, True])
def test_quality_accepts_all_sibling_results_of_selected_exchange(selected, multiple):
    spec, _, bundle, dossier = _inputs(selected, multiple=multiple)
    assert {ref.message_id for ref in bundle.evidence_frontier} == {"caller", "result-a", "result-b"}
    assert len(bundle.source_frontier) == 1
    preflight = ContextQualityVerifier().preflight(spec, bundle, dossier)
    assert preflight.eligible, preflight.reasons
    assert preflight.unused_evidence_keys == ()


def test_quality_still_rejects_genuinely_unrelated_evidence():
    spec, evidence, bundle, dossier = _inputs()
    expanded = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=bundle.items)
    dossier = asyncio.run(DeterministicTestContextSynthesisService().synthesize(None, spec, expanded)).dossier
    assert dossier is not None
    preflight = ContextQualityVerifier().preflight(spec, expanded, dossier)
    extra = evidence.sources[0].messages[-1].ref
    assert not preflight.eligible
    assert preflight.blocker_codes == ("evidence_without_purpose",)
    assert preflight.unused_evidence_keys == (evidence_ref_key(extra),)


def test_selected_result_cannot_hide_missing_sibling():
    spec, evidence, _, _ = _inputs()
    source = evidence.sources[0]
    incomplete = evidence.model_copy(update={"sources": (source.model_copy(update={"messages": tuple(
        message for message in source.messages if message.ref.message_id != "result-b")}),),
        "evidence_frontier": tuple(message.ref for message in source.messages if message.ref.message_id != "result-b")})
    result = MultiSourceEvidenceResolver().resolve(_opportunity(spec, incomplete), (), _corpus(incomplete, {}))
    assert isinstance(result, ExpansionBlocker)
    assert result.code == "required_evidence_unresolved"
    assert "sibling Tool Result" in result.summary


@pytest.mark.parametrize("missing", ["caller", "result-b"])
def test_quality_fails_closed_when_persisted_exchange_is_incomplete(missing):
    spec, _, bundle, _ = _inputs()
    source = bundle.evidence.sources[0]
    messages = tuple(message for message in source.messages if message.ref.message_id != missing)
    evidence = MultiSourceEvidence(sources=(source.model_copy(update={"messages": messages}),),
        evidence_frontier=tuple(message.ref for message in messages))
    incomplete = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=bundle.items)
    dossier = asyncio.run(DeterministicTestContextSynthesisService().synthesize(None, spec, incomplete)).dossier
    assert dossier is not None
    preflight = ContextQualityVerifier().preflight(spec, incomplete, dossier)
    assert not preflight.eligible
    assert "tool_exchange_incomplete" in preflight.blocker_codes
