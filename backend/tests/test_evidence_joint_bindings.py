"""本文件对外提供联合证据解析回归。
输入为五个精确Mission条目及跨来源联合绑定；输出为完整集合、预算阻断和精确身份断言。
具体工作流为调用生产resolver，验证准入集合不丢失且闭包预算不裁剪；示例：pytest backend/tests/test_evidence_joint_bindings.py -q。
"""
from backend.app.desktop.agent_loop.context_expansion.contracts import CandidateEvidenceIdentity, EvidenceRequirement, ExpansionBlocker, ResolvedEvidenceBundle, WorkContextSpec, stable_expansion_hash
from backend.app.desktop.agent_loop.context_expansion.evidence_resolver import MultiSourceEvidenceResolver
from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from backend.app.desktop.context_curation import MissionEvidenceRef, MultiSourceEvidence, StructuredEvidence, evidence_ref_key
from backend.tests._semantic_context_fixtures import single_source_fixture, duplicate_message_id_fixture
from backend.tests.test_semantic_evidence_resolver import _corpus, _opportunity, _single_requirement_spec, _unit_id


def _mission_scope():
    refs = tuple(MissionEvidenceRef(identity_version='mission-section-v2', loop_id='loop', goal_revision=1,
        section_kind=section, item_id=item, content_hash=EffectiveMissionProjector.section_hash(f'Exact section {item}')) for section, item in (
            ('outcome', 'outcome'), ('completion_check', 'check-2'), ('boundary', 'in_scope'),
            ('boundary', 'prohibited_actions'), ('boundary', 'required_invariants')))
    candidates = []
    for ref in refs:
        index = 'a' * 64
        entry = stable_expansion_hash('mission-entry', evidence_ref_key(ref))
        candidates.append(CandidateEvidenceIdentity(candidate_id=stable_expansion_hash(
            'retrieval-candidate-v2', index, 'mission_section', entry, ref.content_hash),
            index_id=index, entry_id=entry, entry_kind='mission_section', source_type='mission',
            mission_ref=ref, source_content_hash=ref.content_hash))
    evidence = MultiSourceEvidence(sources=single_source_fixture().sources,
        structured=tuple(StructuredEvidence(ref=ref, content=f'Exact section {ref.item_id}') for ref in refs), evidence_frontier=refs)
    spec = WorkContextSpec.create(planner_version='test', objective='Review the exact full scope', separation_reason='Independent review',
        questions=('What is the full authorized scope?',), completion_criteria=('Scope is retained',), workspace_requirement='read_only',
        evidence_requirements=(EvidenceRequirement(requirement_id='mission-scope', role='requirement', question='What is the full scope?',
            coverage_criterion='Retain outcome, check-2 and all three boundaries', candidate_refs=tuple(candidates)),))
    corpus = _corpus(evidence, {evidence_ref_key(ref): ('requirement',) for ref in refs})
    return spec, evidence, corpus, refs


def test_joint_mission_binding_retains_all_five_exact_sections():
    spec, evidence, corpus, refs = _mission_scope()
    result = MultiSourceEvidenceResolver().resolve(_opportunity(spec, evidence), (), corpus)
    assert isinstance(result, ResolvedEvidenceBundle)
    assert {evidence_ref_key(ref) for ref in result.evidence_frontier} == {evidence_ref_key(ref) for ref in refs}
    assert len(result.items) == 5 and {item.requirement_id for item in result.items} == {'mission-scope'}


def test_budget_cannot_silently_drop_part_of_bound_mission_scope():
    spec, evidence, corpus, _ = _mission_scope()
    result = MultiSourceEvidenceResolver().resolve(_opportunity(spec, evidence), (), corpus, max_items=4)
    assert isinstance(result, ExpansionBlocker) and result.code == 'evidence_budget_exhausted'


def test_joint_requirement_keeps_distinct_frozen_source_bindings():
    evidence = duplicate_message_id_fixture()
    refs = tuple(source.messages[0].ref for source in evidence.sources)
    spec = _single_requirement_spec(role='implementation', candidate_unit_ids=tuple(_unit_id(ref) for ref in refs))
    corpus = _corpus(evidence, {evidence_ref_key(ref): ('implementation',) for ref in refs})
    result = MultiSourceEvidenceResolver().resolve(_opportunity(spec, evidence), (), corpus)
    assert isinstance(result, ResolvedEvidenceBundle)
    assert len(result.source_frontier) == len(result.items) == 2
    assert {evidence_ref_key(ref) for ref in result.evidence_frontier} == {evidence_ref_key(ref) for ref in refs}


def test_same_evidence_reached_by_candidate_and_unit_is_charged_once():
    spec, evidence, corpus, refs = _mission_scope()
    requirement = spec.evidence_requirements[0]
    bound = EvidenceRequirement(
        requirement_id=requirement.requirement_id, role=requirement.role, question=requirement.question,
        coverage_criterion=requirement.coverage_criterion, candidate_refs=requirement.candidate_refs,
        candidate_unit_ids=tuple(_unit_id(ref) for ref in refs))
    combined = WorkContextSpec.create(planner_version=spec.planner_version, objective=spec.objective,
        separation_reason=spec.separation_reason, questions=spec.questions, completion_criteria=spec.completion_criteria,
        workspace_requirement=spec.workspace_requirement, evidence_requirements=(bound,))
    result = MultiSourceEvidenceResolver().resolve(_opportunity(combined, evidence), (), corpus, max_items=5)
    assert isinstance(result, ResolvedEvidenceBundle)
    assert len(result.items) == len(result.evidence_frontier) == 5


def test_exact_identity_fallback_checks_budget_after_complete_selection():
    refs = tuple(MissionEvidenceRef(identity_version='mission-section-v2', loop_id='loop', goal_revision=revision,
        section_kind='boundary', item_id='in_scope',
        content_hash=EffectiveMissionProjector.section_hash(f'Scope revision {revision}')) for revision in (1, 2))
    evidence = MultiSourceEvidence(sources=single_source_fixture().sources,
        structured=tuple(StructuredEvidence(ref=ref, content=f'Scope revision {ref.goal_revision}') for ref in refs),
        evidence_frontier=refs)
    spec = WorkContextSpec.create(planner_version='test', objective='Compare both frozen scope revisions',
        separation_reason='Independent comparison', questions=('What changed?',), completion_criteria=('Compare both scopes',),
        workspace_requirement='read_only', evidence_requirements=(EvidenceRequirement(requirement_id='in_scope',
            role='requirement', question='What changed?', coverage_criterion='Both exact revisions'),))
    corpus = _corpus(evidence, {evidence_ref_key(ref): ('requirement',) for ref in refs})
    result = MultiSourceEvidenceResolver().resolve(_opportunity(spec, evidence), (), corpus, max_items=1)
    assert isinstance(result, ExpansionBlocker) and result.code == 'evidence_budget_exhausted'
