"""本文件对外提供完成动作资格、冻结兼容与候选纠错的生产服务回归。

输入为隔离 PostgreSQL、真实 Loop/Verifier 及受控远端模型响应；输出为候选拒绝、实际消费、最终受理和历史 hash 断言。
具体工作流为独立验证经生产领取与保存，再冻结观察、调用生产候选合同及 Kernel；证据通过真实 fixture 命令产生。
示例：python -m pytest backend/tests/test_loop_completion_eligibility.py；不操作真实 Vault。
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import os
import uuid
import hashlib
import json

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop.completion import CompletionEvidenceService
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDecision, LoopDelegationGrant, LoopObservation, LoopRound, LoopWorkerRequest
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.patrol import PatrolContractViolation
from backend.app.desktop.agent_loop.patrol_contract import PatrolDecisionContract
from backend.app.desktop.agent_loop.schemas import CompletionVerificationContract, RequestCompletionAction, LoopObservationEnvelope
from backend.tests.completion_evidence_support import claim_verifier, record_verified_fixture
from backend.tests.test_agent_loop_round_liveness import _seed_loop, _stop
from backend.tests.test_round_task_progress import _Checkpointer

pytestmark = pytest.mark.usefixtures('runtime_postgres_database')


@asynccontextmanager
async def _lab(tmp_path, *, stale=True, complete=False):
    engine = create_async_engine(os.environ['FOCUS_DATABASE_URL'])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fixture = await _seed_loop(sessions, tmp_path, label='d10', started_at=datetime.now(UTC))
    try:
        worker_id = uuid.uuid4().hex
        async with sessions.begin() as session:
            grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == fixture['loop_id']))
            grant.capabilities = [*grant.capabilities, 'request_completion', 'request_completion_verifier']
            round_row = await session.get(LoopRound, fixture['round_id'])
            session.add(LoopWorkerRequest(worker_request_id=worker_id, loop_id=fixture['loop_id'],
                round_id=fixture['round_id'], kind='completion_verifier', scope={}))
        source = None
        if complete:
            source = await record_verified_fixture(sessions, fixture['loop_id'], fixture['context_id'],
                fixture['round_id'], next(tmp_path.iterdir()), round_row.workspace_revision)
        contract = CompletionVerificationContract(verification_id=uuid.uuid4().hex,
            loop_id=fixture['loop_id'], round_id=fixture['round_id'], goal_revision=1,
            frontier_hash='e' * 64 if stale else round_row.frontier_hash,
            workspace_revision=round_row.workspace_revision,
            criteria=({'check_id': 'tests', 'status': 'satisfied' if complete else 'unknown',
                'evidence': [{'kind': 'fact', 'source_id': source, 'summary': '真实 fixture 通过'}] if source else [],
                'explanation': '检查实际证据'},),
            conclusion='satisfied' if complete else 'unknown', unresolved=() if complete else ('tests',))
        retry = await claim_verifier(sessions, fixture['loop_id'], worker_id)
        await CompletionEvidenceService(sessions).record(contract, worker_id, retry_identity=retry)
        frozen = await LoopObservationService(sessions, _Checkpointer()).capture(fixture['loop_id'], fixture['round_id'])
        action = RequestCompletionAction(action='request_completion', verification_id=contract.verification_id,
            final_context_ids=(fixture['context_id'],), final_slot_id=frozen.workspace['slot_id'])
        yield sessions, fixture, frozen, action
    finally:
        await _stop(fixture['service'], fixture['loop_id'])
        await engine.dispose()


def test_stale_verification_enters_candidate_feedback_even_when_new_verifier_allowed(tmp_path):
    async def run():
        async with _lab(tmp_path) as (_, _, frozen, action):
            assert frozen.completion_admission['allowed']
            with pytest.raises(PatrolContractViolation, match='verification_stale'):
                PatrolDecisionContract().validate(actions=(action,), mission_references=(), observation=frozen)
    asyncio.run(run())


def test_unknown_verification_and_missing_identity_are_not_completion_permission(tmp_path):
    async def run():
        async with _lab(tmp_path, stale=False) as (_, _, frozen, action):
            for selected, code in ((action, 'criteria_not_satisfied'),
                (action.model_copy(update={'verification_id': uuid.uuid4().hex}), 'verification_missing')):
                with pytest.raises(PatrolContractViolation, match=code):
                    PatrolDecisionContract().validate(actions=(selected,), mission_references=(), observation=frozen)
            entry, = frozen.completion_eligibility['verifications']
            assert not entry['allowed'] and 'required_checks_missing' in entry['reasons']
    asyncio.run(run())


@pytest.mark.parametrize('exhaust', [False, True])
def test_stale_candidate_uses_existing_bounded_feedback_and_actual_receipts(tmp_path, monkeypatch, exhaust):
    from langchain_core.messages import AIMessage
    from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
    from backend.app.desktop.agent_loop.kernel import LoopKernel
    from backend.app.desktop.agent_loop.round_orchestration import LoopRoundOrchestrator
    from backend.app.desktop.agent_loop.patrol_session_models import LoopPatrolSession
    from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
    from backend.tests.config_helpers import app_config_for, ToolCapableFakeChatModel

    async def run():
        async with _lab(tmp_path) as (sessions, fixture, frozen, action):
            calls = []
            class Provider(ToolCapableFakeChatModel):
                async def ainvoke(self, messages, config):
                    calls.append(messages)
                    config['callbacks'][0].usage_metadata['d10-model'] = {'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30}
                    chosen = action.model_dump(mode='json') if exhaust or len(calls) == 1 else {
                        'action': 'request_completion_verifier', 'candidate_context_ids': [fixture['context_id']]}
                    return AIMessage(content=json.dumps({'rationale': '根据冻结资格选择',
                        'mission_references': [{'role': 'outcome', 'reference_id': 'outcome'},
                            {'role': 'completion_check', 'reference_id': 'tests'}], 'actions': [chosen]}))
            model = Provider()
            monkeypatch.setattr('backend.app.desktop.agent_loop.round_orchestration.create_chat_model', lambda **kwargs: model)
            config = app_config_for('d10-model', None)
            config.models[0].curation_output_method = 'prompt_json'
            orchestrator = LoopRoundOrchestrator(sessions, config, LoopKernel(sessions), _Checkpointer())
            claim = await LoopCoordinator(sessions).claim_for_loop(fixture['loop_id'], 'd10-patrol')
            await orchestrator._patrol_sessions.begin(claim)
            if exhaust:
                with pytest.raises(PatrolContractViolation, match='verification_stale'):
                    await orchestrator._decide_patrol(claim, None, frozen.grant['holder_id'], frozen)
            else:
                corrected = await orchestrator._decide_patrol(claim, None, frozen.grant['holder_id'], frozen)
                assert corrected.actions[0].action == 'request_completion_verifier'
            assert len(calls) == (3 if exhaust else 2)
            assert 'verification_stale' in str(calls[1])
            async with sessions() as session:
                usage = await session.get(LoopBudgetUsage, fixture['loop_id'])
                assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (len(calls), 20 * len(calls), 10 * len(calls))
                receipts = tuple(await session.scalars(select(LoopIndexBudgetReservation).where(
                    LoopIndexBudgetReservation.loop_id == fixture['loop_id'])))
                assert len(receipts) == len(calls) and all(row.settled_at for row in receipts)
                saved = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == frozen.round_id))
                assert saved.envelope['completion_eligibility'] == frozen.completion_eligibility
                assert await session.scalar(select(func.count()).select_from(LoopDecision).where(LoopDecision.round_id == frozen.round_id)) == 0
                if exhaust:
                    loop = await session.get(AgentLoop, fixture['loop_id'])
                    patrol = await session.scalar(select(LoopPatrolSession).where(LoopPatrolSession.round_id == frozen.round_id))
                    assert loop.status == 'waiting_user' and patrol.status == 'failed'
                    assert (await session.get(LoopRound, frozen.round_id)).status == 'error'
    asyncio.run(run())


def test_credible_complete_verification_passes_candidate_stage_and_kernel(tmp_path):
    from backend.app.desktop.agent_loop.context_expansion.coordinator import ContextExpansionStage
    from backend.app.desktop.agent_loop.kernel import LoopKernel
    from backend.tests.test_loop_round_progress_admission import _intent

    async def run():
        async with _lab(tmp_path, stale=False, complete=True) as (sessions, fixture, frozen, action):
            PatrolDecisionContract().validate(actions=(action,), mission_references=(), observation=frozen)
            intent = _intent(frozen).model_copy(update={'actions': (action,)})
            stage, assessed = await _stage_observation(sessions, frozen)
            resolved = await stage.resolve(assessed, intent)
            assert resolved.blocker is None and resolved.intent.actions == (action,)
            result = await LoopKernel(sessions).commit(resolved.intent)
            assert result.status == 'committed', result.reason
            async with sessions() as session:
                assert (await session.get(AgentLoop, fixture['loop_id'])).status == 'completed'
    asyncio.run(run())


@pytest.mark.parametrize('change', ['workspace', 'source', 'run', 'pause', 'revoke', 'frontier', 'mission'])
def test_final_state_changes_reject_previously_eligible_frozen_candidate(tmp_path, change):
    from backend.app.desktop.agent_loop.context_expansion.coordinator import ContextExpansionStage
    from backend.app.desktop.agent_loop.kernel import LoopKernel
    from backend.app.desktop.models import DesktopRun, DesktopThread
    from backend.app.desktop.domain_evidence.models import DesktopDomainResult
    from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
    from backend.tests.test_loop_round_progress_admission import _intent

    async def run():
        async with _lab(tmp_path, stale=False, complete=True) as (sessions, fixture, frozen, action):
            PatrolDecisionContract().validate(actions=(action,), mission_references=(), observation=frozen)
            intent = _intent(frozen).model_copy(update={'actions': (action,)})
            stage, assessed = await _stage_observation(sessions, frozen)
            assert (await stage.resolve(assessed, intent)).blocker is None
            if change == 'pause':
                await fixture['service'].control(fixture['loop_id'], 'pause')
            else:
                async with sessions.begin() as session:
                    if change == 'workspace':
                        (await session.get(WorkspaceSlot, action.final_slot_id)).revision += 1
                    elif change == 'source':
                        source = await session.scalar(select(DesktopDomainResult).where(DesktopDomainResult.loop_id == fixture['loop_id']))
                        source.payload = {**source.payload, 'status': 'failed'}
                    elif change == 'run':
                        session.add(DesktopRun(run_id=uuid.uuid4().hex, task_id=fixture['context_id'], agent_id='main:' + fixture['context_id'], kind='worker',
                            status='running', loop_id=fixture['loop_id'], round_id=fixture['round_id']))
                    elif change == 'revoke':
                        (await session.get(LoopDelegationGrant, frozen.grant['grant_id'])).status = 'revoked'
                    elif change == 'frontier':
                        (await session.get(DesktopThread, fixture['context_id'])).current_revision_id = None
                    else:
                        (await session.get(AgentLoop, fixture['loop_id'])).goal_revision += 1
            assert (await stage.resolve(assessed, intent)).blocker is not None
            result = await LoopKernel(sessions).commit(intent)
            assert result.status in ('rejected', 'superseded'), result.reason
            async with sessions() as session:
                assert (await session.get(AgentLoop, fixture['loop_id'])).status != 'completed'
                saved = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == frozen.round_id))
                assert saved.envelope['completion_eligibility'] == frozen.completion_eligibility
    asyncio.run(run())


async def _stage_observation(sessions, frozen):
    from backend.app.desktop.agent_loop.context_expansion.coordinator import ContextExpansionStage

    stage = ContextExpansionStage(sessions, _Checkpointer())
    assessment = await stage.assess(frozen)
    return stage, frozen.model_copy(update={'expansion_assessment': assessment.model_dump(mode='json')})


def test_historical_absent_eligibility_keeps_original_hash(tmp_path):
    from backend.app.desktop.agent_loop.observation import observation_hash

    async def run():
        async with _lab(tmp_path) as (_, _, frozen, _):
            historical = frozen.model_dump(mode='json')
            historical.pop('completion_eligibility')
            expected = hashlib.sha256(json.dumps(historical, ensure_ascii=False, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest()
            restored = LoopObservationEnvelope.model_validate(historical)
            assert restored.completion_eligibility is None
            assert observation_hash(restored) == expected
    asyncio.run(run())
