"""本文件对外提供质量恢复的冻结合同回归。
输入为三维判定与真实类型化WorkSpec、bundle和dossier；输出为原判定复原及异源缓存阻断断言。
具体工作流为构造合法持久评估，再从同输入或非法身份恢复，不调用Provider；示例：pytest backend/tests/test_context_quality_restore_contract.py -q。
"""
import asyncio

import pytest

from backend.app.desktop.agent_loop.context_expansion.quality import ContextQualityAssessment, ContextQualityVerifier
from backend.app.desktop.agent_loop.context_expansion.contracts import stable_expansion_hash
from backend.app.desktop.agent_loop.context_expansion.synthesis_service import DeterministicTestContextSynthesisService
from backend.tests.test_synthesis_claim_feedback import _inputs


def _assessment(dimension='minimality', verdict='pass'):
    spec, bundle = _inputs()
    synthesis = asyncio.run(DeterministicTestContextSynthesisService().synthesize(None, spec, bundle))
    dossier = synthesis.dossier
    assert dossier is not None
    verifier = ContextQualityVerifier()
    result = verifier.verify(spec, bundle, dossier, {'dimensions': [
        {'dimension': name, 'verdict': verdict if name == dimension else 'pass', 'reasons': ['Controlled restore contract']}
        for name in ('minimality', 'sufficiency', 'coherence')]})
    assert result.assessment is not None
    return spec, bundle, dossier, result


@pytest.mark.parametrize('dimension', ['minimality', 'sufficiency', 'coherence'])
@pytest.mark.parametrize('verdict', ['pass', 'fail', 'unknown'])
def test_restore_preserves_every_saved_dimension(dimension, verdict):
    spec, bundle, dossier, original = _assessment(dimension, verdict)
    restored = ContextQualityVerifier().restore(spec, bundle, dossier, original.assessment.model_dump(mode='json'))
    assert restored == original
    assert restored.attempt_records == ()


@pytest.mark.parametrize('field,value', [('policy_version', 'other-policy'), ('verifier_version', 'other-verifier'),
                                       ('work_spec_id', 'b' * 64), ('preflight_id', 'c' * 64)])
def test_restore_rejects_foreign_but_self_consistent_assessment(field, value):
    spec, bundle, dossier, original = _assessment()
    payload = original.assessment.model_dump(mode='json')
    payload[field] = value
    payload['assessment_id'] = stable_expansion_hash('context-quality-assessment', payload['work_spec_id'],
        payload['resolution_id'], payload['dossier_id'], payload['preflight_id'], payload['verifier_version'],
        payload['policy_version'], tuple(payload['dimensions']))
    ContextQualityAssessment.model_validate(payload)
    restored = ContextQualityVerifier().restore(spec, bundle, dossier, payload)
    assert restored.blocker_code == 'quality_contract_invalid' and restored.assessment is None


def test_restore_revalidates_verdict_membership_not_only_stable_hash():
    spec, bundle, dossier, original = _assessment()
    payload = original.assessment.model_dump(mode='json')
    payload['dimensions'][0]['claim_ids'] = ['d' * 64]
    payload['assessment_id'] = stable_expansion_hash('context-quality-assessment', payload['work_spec_id'],
        payload['resolution_id'], payload['dossier_id'], payload['preflight_id'], payload['verifier_version'],
        payload['policy_version'], tuple(payload['dimensions']))
    ContextQualityAssessment.model_validate(payload)
    restored = ContextQualityVerifier().restore(spec, bundle, dossier, payload)
    assert restored.blocker_code == 'quality_contract_invalid' and restored.assessment is None


def test_restore_rejects_malformed_saved_assessment():
    spec, bundle, dossier, _ = _assessment()
    restored = ContextQualityVerifier().restore(spec, bundle, dossier, {'assessment_id': 'invalid'})
    assert restored.blocker_code == 'quality_contract_invalid' and restored.assessment is None
