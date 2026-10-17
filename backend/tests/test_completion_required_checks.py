"""完成验收范围回归：动态合同通过后，仅必需项阻止完成。"""

import pytest
from pydantic import ValidationError

from backend.app.desktop.agent_loop.completion import CompletionGuard
from backend.app.desktop.agent_loop.completion_policy import CompletionCheckPolicy
from backend.app.desktop.agent_loop.schemas import CompletionVerificationContract


def _checks():
    return (
        {'check_id': 'tests', 'required': True, 'expected_evidence_kinds': ['test']},
        {'check_id': 'polish', 'required': False, 'expected_evidence_kinds': ['test']},
    )


def _verification(checks, *, required_status='satisfied', optional_status='unknown', unresolved=(), conclusion='satisfied'):
    proposal = CompletionCheckPolicy.schema_for(checks).model_validate({
        'criteria': [
            {'check_id': 'tests', 'status': required_status, 'explanation': '必需项检查',
             'evidence': [{'kind': 'test', 'source_id': 'verified-tests'}] if required_status == 'satisfied' else []},
            {'check_id': 'polish', 'status': optional_status, 'explanation': '可选项检查',
             'evidence': [{'kind': 'test', 'source_id': 'verified-polish'}] if optional_status == 'satisfied' else []},
        ],
        'conclusion': conclusion, 'unresolved': unresolved,
    })
    CompletionCheckPolicy().validate(checks, proposal.criteria)
    return CompletionVerificationContract(
        verification_id='v', loop_id='l', round_id='r', goal_revision=1,
        frontier_hash='a' * 64, workspace_revision=1, **proposal.model_dump())


def _guard(checks, verification, **overrides):
    facts = dict(goal_revision=1, frontier_hash='a' * 64, workspace_revision=1,
                 active_or_queued_runs=0, pending_human_gates=0, portfolio_published=True,
                 workspace_adopted=True, grant_allows_completion=True)
    return CompletionGuard().check(verification, checks=checks, **(facts | overrides))


@pytest.mark.parametrize('optional_status', ['unknown', 'unsatisfied', 'satisfied'])
@pytest.mark.parametrize('unresolved', [(), ('polish',)])
def test_optional_unknown_does_not_block_completion(optional_status, unresolved):
    checks = _checks()
    result = _guard(checks, _verification(checks, optional_status=optional_status, unresolved=unresolved))
    assert result.allowed, result.reasons
    assert not result.waiting_user


@pytest.mark.parametrize('required_status', ['unknown', 'unsatisfied'])
@pytest.mark.parametrize('explicit_required', [True, False])
def test_required_check_blocks_even_when_conclusion_claims_satisfied(required_status, explicit_required):
    checks = list(_checks())
    if not explicit_required:
        checks[0] = {k: v for k, v in checks[0].items() if k != 'required'}
    result = _guard(checks, _verification(checks, required_status=required_status))
    assert not result.allowed
    assert {'criteria_not_satisfied', 'required_checks_missing'} <= set(result.reasons)


def test_unresolved_required_check_still_blocks_even_when_criterion_is_satisfied():
    checks = _checks()
    result = _guard(checks, _verification(checks, unresolved=('tests', 'polish')))
    assert result.reasons == ('unresolved_items',)


@pytest.mark.parametrize('conclusion', ['satisfied', 'unknown', 'unsatisfied'])
def test_all_optional_requires_explicit_satisfied_conclusion(conclusion):
    checks = tuple({**c, 'required': False} for c in _checks())
    verification = _verification(checks, required_status='unknown', optional_status='unsatisfied',
                                 unresolved=('tests', 'polish'), conclusion=conclusion)
    result = _guard(checks, verification)
    assert result.allowed is (conclusion == 'satisfied')


@pytest.mark.parametrize('facts,reason', [
    ({'goal_revision': 2}, 'verification_stale'),
    ({'frontier_hash': 'b' * 64}, 'verification_stale'),
    ({'workspace_revision': 2}, 'verification_stale'),
    ({'active_or_queued_runs': 1}, 'runs_active'),
    ({'pending_human_gates': 1}, 'human_gate_pending'),
    ({'portfolio_published': False}, 'portfolio_not_published'),
    ({'workspace_adopted': False}, 'workspace_not_adopted'),
    ({'grant_allows_completion': False}, 'grant_disallows_completion'),
])
def test_all_optional_still_obeys_other_completion_gates(facts, reason):
    checks = tuple({**c, 'required': False} for c in _checks())
    result = _guard(checks, _verification(checks, required_status='unknown'), **facts)
    assert not result.allowed and reason in result.reasons


def test_empty_declarations_and_missing_optional_results_are_invalid():
    checks = _checks()
    verification = _verification(checks)
    assert 'verification_checks_invalid' in _guard((), verification).reasons
    missing = verification.model_copy(update={'criteria': verification.criteria[:1]})
    assert 'verification_checks_invalid' in _guard(checks, missing).reasons


def test_optional_satisfied_still_requires_allowed_typed_evidence():
    checks = _checks()
    verification = _verification(checks, optional_status='satisfied')
    raw = verification.model_dump()
    raw['criteria'][1]['evidence'] = ()
    with pytest.raises(ValidationError, match='类型化证据'):
        CompletionVerificationContract.model_validate(raw)
    raw = verification.model_dump()
    raw['criteria'][1]['evidence'][0]['kind'] = 'workspace'
    invalid = CompletionVerificationContract.model_validate(raw)
    assert 'verification_checks_invalid' in _guard(checks, invalid).reasons
