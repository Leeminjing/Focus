"""本文件对外提供正式作者冻结引用选择和权威形状合同回归。
输入为六类原类型化引用、生产 RoleBound 请求及合法或非法作者声明；输出为精确目录枚举、原身份恢复与拒绝断言。
具体工作流为冻结目录进入实际请求 Schema，选择绑定回原引用，物化与持久 DTO 往返保持，未知来源及无根图不进入 Provider。
示例：pytest backend/tests/test_synthesis_authoring_contract.py -q；受控远端响应只检验协议，不替代真实支持核验或正式派生。
"""
import asyncio
import json

import pytest
from pydantic import TypeAdapter, ValidationError

import backend.app.desktop.agent_loop.structured_worker as worker_module
from backend.app.desktop.agent_loop.context_expansion.contracts import ResolvedEvidenceBundle
from backend.app.desktop.agent_loop.context_expansion.synthesis import ContextSynthesisWorkerDraft
from backend.app.desktop.context_curation import (
    EvidenceRef, MaterialEvidenceRef, MissionEvidenceRef, MultiSourceEvidence, NamespacedMessageRef,
    RunResultEvidenceRef, StructuredEvidence, WorkspaceEffectEvidenceRef, evidence_ref_key,
)
from backend.tests._semantic_context_fixtures import single_source_fixture
from backend.tests.test_synthesis_claim_feedback import _draft, _inputs, _service


def _selection(spec, bundle):
    payload = _draft(spec, bundle)
    payload['sections'][0]['claims'][0]['citations'] = [next(iter(ContextSynthesisWorkerDraft.citation_catalog(bundle)))]
    return payload


@pytest.mark.parametrize('ref', [
    single_source_fixture().evidence_frontier[0],
    MissionEvidenceRef(loop_id='loop', goal_revision=1, item_id='check', content_hash='a' * 64),
    MissionEvidenceRef(identity_version='mission-section-v2', loop_id='loop', goal_revision=1,
        section_kind='outcome', item_id='outcome', content_hash='a' * 64),
    RunResultEvidenceRef(run_id='run', context_id='context', result_id='result', content_hash='a' * 64),
    MaterialEvidenceRef(material_id='material', version_id='version', content_hash='a' * 64),
    WorkspaceEffectEvidenceRef(workspace_id='workspace', revision=1, effect_id='effect', content_hash='a' * 64),
])
def test_frozen_selection_preserves_typed_reference_claim_identity_and_canonical_json(ref):
    spec, original = _inputs()
    evidence = original.evidence if isinstance(ref, NamespacedMessageRef) else MultiSourceEvidence(
        sources=original.evidence.sources, structured=(StructuredEvidence(ref=ref, content='Frozen state'),), evidence_frontier=(ref,))
    bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence,
        items=(original.items[0].model_copy(update={'ref': ref}),))
    selected = ContextSynthesisWorkerDraft.schema_for(bundle).model_validate(_selection(spec, bundle))
    canonical = ContextSynthesisWorkerDraft.model_validate(_draft(spec, bundle))
    assert selected.sections[0].claims[0].citations == (ref,)
    assert type(selected.sections[0].claims[0].citations[0]) is type(ref)
    assert selected.materialize() == canonical.materialize()
    assert ContextSynthesisWorkerDraft.model_validate_json(selected.model_dump_json()).materialize() == selected.materialize()


@pytest.mark.parametrize('invalid', ['unknown', 'object', 'confirmed-empty', 'confirmed-premise', 'inference-empty', 'hypothesis-empty'])
def test_unknown_source_and_authority_lineage_rejected_at_author_parse(invalid):
    spec, bundle = _inputs()
    payload = _selection(spec, bundle)
    claim = payload['sections'][0]['claims'][0]
    if invalid == 'unknown':
        claim['citations'] = ['outside-frozen-catalog']
    elif invalid == 'object':
        claim['citations'] = [bundle.evidence_frontier[0].model_dump(mode='json')]
    elif invalid == 'confirmed-premise':
        claim['premise_claim_keys'] = ['state']
    else:
        claim.update(citations=[], premise_claim_keys=[], authority={
            'confirmed-empty': 'confirmed', 'inference-empty': 'inference', 'hypothesis-empty': 'hypothesis'}[invalid])
    with pytest.raises(ValidationError):
        ContextSynthesisWorkerDraft.schema_for(bundle).model_validate(payload)


@pytest.mark.parametrize('authority,with_citation,with_premise', [
    ('inference', False, True), ('inference', True, True),
    ('hypothesis', True, False), ('hypothesis', True, True), ('hypothesis', False, True),
])
def test_existing_legal_lineage_preserved(authority, with_citation, with_premise):
    spec, bundle = _inputs()
    payload = _selection(spec, bundle)
    root = payload['sections'][0]['claims'][0]
    child = {**root, 'claim_key': 'child', 'statement': 'Dependent state.', 'authority': authority,
        'citations': root['citations'] if with_citation else [], 'premise_claim_keys': ['state'] if with_premise else []}
    payload['sections'][0]['claims'].append(child)
    worker = ContextSynthesisWorkerDraft.schema_for(bundle).model_validate(payload)
    assert worker.materialize() == ContextSynthesisWorkerDraft.model_validate(worker.model_dump(mode='json')).materialize()


@pytest.mark.parametrize('premise', ['missing', 'child'])
def test_source_selection_does_not_relax_unknown_premise_or_cycle(premise):
    spec, bundle = _inputs()
    payload = _selection(spec, bundle)
    payload['sections'][0]['claims'].append({'claim_key': 'child', 'statement': 'Dependent state.',
        'authority': 'inference', 'premise_claim_keys': [premise]})
    worker = ContextSynthesisWorkerDraft.schema_for(bundle).model_validate(payload)
    with pytest.raises(ValueError, match='已知|无环'):
        worker.materialize()


def test_empty_frozen_catalog_blocks_before_any_provider_or_attempt(monkeypatch):
    async def run():
        service, spec, original, synthesis, verifier, calls = _service(monkeypatch)
        bundle = original.model_copy(update={'evidence_frontier': ()})
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is None and result.review is None and result.blocker_code == 'synthesis_invalid'
        assert '缺少冻结 citation' in result.blocker_summary
        assert calls == [] and result.attempt_records == ()
        assert synthesis.last_usage.model_calls == verifier.last_usage.model_calls == 0
    asyncio.run(run())


def test_actual_author_request_has_exact_frozen_catalog_and_shared_enum(monkeypatch):
    async def run():
        service, spec, bundle, _, _, calls = _service(monkeypatch)
        factory = worker_module.create_chat_model
        contracts = []

        class Provider:
            async def ainvoke(self, messages, config):
                body = messages[-1].content
                if '"work_spec"' in body:
                    contracts.append(json.loads(body.split('JSON Schema: ', 1)[1]))
                return await factory().ainvoke(messages, config)

        monkeypatch.setattr(worker_module, 'create_chat_model', lambda **kwargs: Provider())
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is not None, result.blocker_summary
        catalog = calls[0][1]['citation_catalog']
        assert {evidence_ref_key(TypeAdapter(EvidenceRef).validate_python(ref)) for ref in catalog.values()} == {
            evidence_ref_key(ref) for ref in bundle.evidence_frontier}
        contract = contracts[0]
        definitions = contract['$defs']
        section = definitions[contract['properties']['sections']['items']['$ref'].rsplit('/', 1)[-1]]
        variants = section['properties']['claims']['items']['anyOf']
        assert len(variants) == 4
        citation_refs = {definitions[item['$ref'].rsplit('/', 1)[-1]]['properties']['citations']['items']['$ref'] for item in variants}
        assert len(citation_refs) == 1
        choices = definitions[next(iter(citation_refs)).rsplit('/', 1)[-1]]
        assert choices['type'] == 'string' and set(choices['enum']) == set(catalog)
        assert [role for role, _ in calls] == ['synthesis', 'verifier']
    asyncio.run(run())
