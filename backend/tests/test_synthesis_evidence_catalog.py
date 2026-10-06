"""本文件对外提供独立核验请求的共享冻结来源目录回归。
输入为生产RoleBound模型、重复引用的声明及同正文不同身份来源；输出为完整证据只内联一次和身份关联保真的断言。
具体工作流为只替换远端响应，捕获实际Provider请求，逐项重建声明的引用并核对来源角色、正文和未引用来源排除。
完整请求容量仍经原IndexModelBudget准入，旧重复请求超窗而完整共享目录可容纳时只执行原一次独立核验。
示例：pytest backend/tests/test_synthesis_evidence_catalog.py -q；受控判定用于检验协议，不代替原生支持或发布验收。
"""
import asyncio
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage
import pytest

import backend.app.desktop.agent_loop.structured_worker as worker_module
from backend.app.desktop.agent_loop.context_expansion.contracts import ResolvedEvidenceBundle
from backend.app.desktop.agent_loop.context_expansion.index_model_budget import BudgetedIndexModel, IndexBudgetExceeded, IndexModelBudget
from backend.app.desktop.agent_loop.context_expansion.synthesis import ClaimSupportProposal
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import StructuredContextSynthesisService
from backend.app.desktop.context_curation import evidence_ref_key
from backend.tests.test_synthesis_claim_feedback import _service


def _capture_many_claims(monkeypatch, count, *, extra_ref=None):
    factory = worker_module.create_chat_model

    class Provider:
        async def ainvoke(self, messages, config):
            document = json.loads(messages[-1].content.split('<worker_input>', 1)[1].split('</worker_input>', 1)[0])
            response = await factory().ainvoke(messages, config)
            payload = json.loads(response.content)
            if 'sections' in payload:
                template = payload['sections'][0]['claims'][0]
                payload['sections'][0]['claims'] = [
                    {**template, 'claim_key': f'fact-{index}', 'statement': f'Frozen fact {index}.'}
                    for index in range(count)
                ]
                if extra_ref is not None:
                    key = next(key for key, ref in document['citation_catalog'].items() if ref == extra_ref.model_dump(mode='json'))
                    payload['sections'][0]['claims'][-1]['citations'].append(key)
            else:
                payload['assessments'] = [
                    {'claim_id': claim['claim_id'], 'verdict': 'supported', 'reason': 'Controlled protocol response'}
                    for claim in document['claims']
                ]
            return AIMessage(content=json.dumps(payload))

    monkeypatch.setattr(worker_module, 'create_chat_model', lambda **kwargs: Provider())


@pytest.mark.parametrize('claim_count', [2, 40])
def test_shared_frozen_body_is_present_once_in_actual_verifier_request(monkeypatch, claim_count):
    async def run():
        service, spec, original, synthesis, verifier, calls = _service(monkeypatch)
        source = original.evidence.sources[0]
        content = 'FULL_FROZEN_SOURCE_BOUNDARY ' + 'complete evidence ' * 1000
        message = source.messages[0].model_copy(update={'content': content})
        evidence = original.evidence.model_copy(update={'sources': (source.model_copy(update={'messages': (message,)}),)})
        bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=original.items)
        _capture_many_claims(monkeypatch, claim_count)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is not None, result.blocker_summary
        request = calls[-1][1]
        assert json.dumps(request).count(content) == 1
        assert len(request['claims']) == claim_count and len(request['evidence']) == 1
        citation = request['evidence'][0]
        assert citation['identity'] == list(evidence_ref_key(message.ref))
        assert citation['content'] == content
        assert citation['source'] == message.model_dump(mode='json', exclude={'content'})
        assert all(claim['citations'] == [citation['identity']] for claim in request['claims'])
        assert [role for role, _ in calls] == ['synthesis', 'verifier']
        assert synthesis.last_usage.model_calls == verifier.last_usage.model_calls == 1
    asyncio.run(run())


def test_same_body_different_source_identity_is_not_coalesced_or_supplemented(monkeypatch):
    async def run():
        service, spec, original, _, _, calls = _service(monkeypatch)
        source = original.evidence.sources[0]
        first = source.messages[0]
        second = first.model_copy(update={'ref': first.ref.model_copy(update={'message_id': 'independent-ai'}), 'role': 'ai'})
        uncited = first.model_copy(update={'ref': first.ref.model_copy(update={'message_id': 'uncited'}), 'content': 'Uncited body'})
        evidence = original.evidence.model_copy(update={
            'sources': (source.model_copy(update={'messages': (first, second, uncited)}),),
            'evidence_frontier': (first.ref, second.ref, uncited.ref),
        })
        bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=original.items)
        _capture_many_claims(monkeypatch, 2, extra_ref=second.ref)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is not None, result.blocker_summary
        request = calls[-1][1]
        catalog = {tuple(item['identity']): item for item in request['evidence']}
        assert set(catalog) == {evidence_ref_key(first.ref), evidence_ref_key(second.ref)}
        assert {item['source']['role'] for item in catalog.values()} == {'human', 'ai'}
        for claim, sent in zip(result.dossier.claims, request['claims'], strict=True):
            assert sent['claim_id'] == claim.claim_id
            assert sent['citations'] == [list(evidence_ref_key(ref)) for ref in claim.citations]
        assert 'Uncited body' not in json.dumps(request)
    asyncio.run(run())


def test_complete_shared_evidence_admitted_without_increasing_request_capacity(monkeypatch):
    async def run():
        service, spec, original, _, verifier, calls = _service(monkeypatch)
        source = original.evidence.sources[0]
        message = source.messages[0].model_copy(update={'content': 'complete evidence ' * 1000})
        evidence = original.evidence.model_copy(update={'sources': (source.model_copy(update={'messages': (message,)}),)})
        bundle = ResolvedEvidenceBundle.create(work_spec=spec, evidence=evidence, items=original.items)
        budget = IndexModelBudget(SimpleNamespace(
            global_model_calls_remaining=1, global_input_tokens_remaining=1000000,
            global_output_tokens_remaining=1000000,
            policy=SimpleNamespace(output_token_reserve=100, max_request_input_tokens=100000),
        ))
        service._claim_verifier_model = BudgetedIndexModel(verifier, budget)
        _capture_many_claims(monkeypatch, 40)
        result = await service.synthesize(None, spec, bundle)
        assert result.dossier is not None, result.blocker_summary
        claims = result.dossier.claims
        old_request = {'claims': tuple({
            'claim_id': claim.claim_id, 'statement': claim.statement,
            'citations': tuple(StructuredContextSynthesisService._citation_payload(bundle, ref) for ref in claim.citations),
        } for claim in claims)}
        schema = ClaimSupportProposal.schema_for(tuple(claim.claim_id for claim in claims))
        with pytest.raises(IndexBudgetExceeded, match='provider_request_window exceeded'):
            budget.require_fit(verifier, schema, service._verification_authority(), old_request)
        assert calls[-1][1]['evidence'][0]['content'] == message.content
        assert [role for role, _ in calls] == ['synthesis', 'verifier']
        assert verifier.last_usage.model_calls == 1
    asyncio.run(run())
