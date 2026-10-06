"""本文件对外提供 D9 Round 推进与完成请求准入的真实 PostgreSQL 回归。

输入为隔离数据库、正式 Loop/冻结 Observation、Kernel 与 Worker/协调者；输出为无 Run 计数、一次推进及重复验收零受理断言。
具体工作流为经生产 Kernel 请求并由实际 Worker 保存 unknown，再驱动不同推进入口核对领域事实。
语义回归排除报告输出顺序及随机身份，旧 v1 Worker 输入重新投影不产生重复验证。
历史哈希基线排除两项新增缺省完成字段，保持原冻结字段集合。
示例：pytest backend/tests/test_loop_round_progress_admission.py；脚本化 Provider 只替代远端响应。
"""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import uuid

import pytest
from sqlalchemy import func, select

from backend.app.desktop.agent_loop.coordinator import LoopCoordinator
from backend.app.desktop.agent_loop.kernel import LoopKernel
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDelegationGrant, LoopRound, LoopWorkerRequest
from backend.app.desktop.agent_loop.schemas import PatrolDecisionIntent
from backend.tests.test_loop_worker_recovery import _case, _step


pytestmark = pytest.mark.usefixtures('runtime_postgres_database')


async def _prepare(sessions, fixture, identity):
    async with sessions.begin() as session:
        await session.delete(await session.get(LoopWorkerRequest, identity))
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == fixture['loop_id'], LoopDelegationGrant.status == 'active'))
        grant.capabilities = [*grant.capabilities, 'request_completion_verifier']


def _intent(observation):
    return PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key='d9:' + observation.round_id,
        loop_id=observation.loop_id, loop_revision=observation.loop_revision, round_id=observation.round_id,
        holder_id=observation.grant['holder_id'], grant_id=observation.grant['grant_id'],
        grant_revision=observation.authority_revision, goal_revision=observation.goal_revision,
        observed_frontier_hash=observation.observed_frontier_hash,
        observed_workspace_revision=observation.workspace['revision'],
        observed_projection_sequence=observation.projection_sequence,
        base_entity_revisions=observation.base_entity_revisions,
        rationale='核验当前可信证据', actions=({'action': 'request_completion_verifier',
            'candidate_context_ids': [item['context_id'] for item in observation.portfolio_frontier]},))


async def _publish_memory(sessions, loop_id):
    from backend.app.desktop.agent_loop.task_progress.runtime import TaskProgressRuntime
    from backend.app.desktop.agent_loop.task_progress.contracts import SourceAssessment
    from focus.runtime.runs.usage import ModelUsage

    class MemoryModel:
        context_window_tokens = 100000
        max_output_tokens = 1000
        last_usage_reported = True
        last_usage = ModelUsage(model_calls=1, input_tokens=17, output_tokens=9)

        async def invoke(self, schema, system, payload):
            return schema(source_assessments=tuple(SourceAssessment(source_key=s['source_key'],
                disposition='unknown', explanation='保留未知来源') for s in payload['task_delta']['sources']))

    assert await TaskProgressRuntime(sessions, None, model_factory=lambda name: MemoryModel()).drain(loop_id=loop_id) == 1


@pytest.mark.parametrize('entry', ['worker', 'coordinator', 'maintenance', 'race', 'successful_run'])
def test_stable_worker_only_round_counts_no_progress_once(tmp_path, monkeypatch, entry):
    async def run():
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            await _prepare(sessions, fixture, identity)
            result = await LoopKernel(sessions).commit(_intent(observation))
            assert result.status == 'committed', result.reason
            provider.valid = True
            original_advance = runtime._advance
            if entry != 'worker':
                async def hold(*args, **kwargs):
                    return None
                monkeypatch.setattr(runtime, '_advance', hold)
            await _step(runtime, fixture['loop_id'])
            if entry != 'worker':
                from backend.app.desktop.agent_loop.rounds import settle_round
                async with sessions.begin() as session:
                    loop = await session.get(AgentLoop, fixture['loop_id'], with_for_update=True)
                    current = await session.get(LoopRound, observation.round_id, with_for_update=True)
                    assert await settle_round(session, loop, current)
                    if entry in {'coordinator', 'successful_run'}:
                        settled_run = None
                        if entry == 'successful_run':
                            from backend.app.desktop.models import DesktopRun

                            settled_run = DesktopRun(run_id=uuid.uuid4().hex, task_id=fixture['context_id'],
                                agent_id=uuid.uuid4().hex, kind='worker', status='success',
                                loop_id=loop.loop_id, round_id=current.round_id,
                                workspace_result={'revision': current.workspace_revision, 'workspace_status': 'settled'},
                                settled_at=datetime.now(UTC))
                            session.add(settled_run)
                            await session.flush()
                        await LoopCoordinator(sessions)._continue_settled_round(session, loop, current, settled_run)
                if entry == 'maintenance':
                    await LoopCoordinator(sessions).maintain_rounds()
                elif entry == 'race':
                    await asyncio.gather(original_advance(fixture['loop_id'], observation.round_id),
                                         LoopCoordinator(sessions).maintain_rounds())
            await original_advance(fixture['loop_id'], observation.round_id)
            async with sessions() as session:
                usage = await session.get(LoopBudgetUsage, fixture['loop_id'])
                assert usage.no_progress_count == 1
                assert usage.rounds == 1
                assert await session.scalar(select(func.count()).select_from(LoopRound).where(LoopRound.loop_id == fixture['loop_id'])) == 2
    asyncio.run(run())


def test_legacy_semantic_version_alone_does_not_count_as_progress(tmp_path, monkeypatch):
    async def run():
        from backend.app.desktop.agent_loop.models import LoopObservation
        from backend.app.desktop.agent_loop.observation import observation_hash
        from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope
        from backend.app.desktop.agent_loop.round_progress import RoundProgressReader
        from backend.app.desktop.agent_loop.rounds import current_frontier_hash

        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            await _prepare(sessions, fixture, identity)
            assert (await LoopKernel(sessions).commit(_intent(observation))).status == 'committed'
            provider.valid = True
            await _step(runtime, fixture['loop_id'])
            async with sessions.begin() as session:
                current = await session.get(LoopRound, observation.round_id)
                saved = await session.get(LoopObservation, current.observation_id)
                envelope = deepcopy(saved.envelope)
                previous = envelope['completion_admission']['input']
                previous['contract'] = previous['semantic']['contract'] = 'completion-semantic-input-v1'
                saved.envelope = envelope
                saved.envelope_hash = observation_hash(LoopObservationEnvelope.model_validate(envelope))
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                state = await RoundProgressReader().read(session, loop, current,
                    await current_frontier_hash(session, loop.loop_id))
                assert not state['progressed']
                assert state['baseline'] == 'legacy_workspace_frontier'
    asyncio.run(run())


def test_semantic_input_ignores_execution_identity_but_preserves_content_and_coverage():
    from backend.app.desktop.agent_loop.completion_admission import CompletionRequestAdmission

    projected = {'mission': {'revision': 1, 'outcome': '交付', 'completion_checks': [{'check_id': 'test'}]},
        'workspace': {'revision': 1, 'fingerprint': 'actual'}, 'frontier_hash': 'frontier',
        'available_source_ids': ['first'], 'completion_sources': [{'kind': 'test', 'source_id': 'first',
            'eligibility': {'status': 'available', 'code': None}, 'payload': {'status': 'verified',
                'metrics': {'passed': 2}, 'execution_proof': {'run_id': 'r1', 'call_id': 'c1',
                    'command': 'npm test', 'status': 'exited', 'exit_code': 0, 'bound': True, 'workspace': 'fixture'},
                'named_results': {'protocol': 'tap', 'status': 'complete', 'report_hash': 'one',
                    'cases': [{'case_id': 'random1', 'name': 'persists prompt', 'suite': ['settings'],
                               'status': 'passed', 'coverage': 'available'},
                              {'case_id': 'random3', 'name': 'restores position', 'suite': ['pet'],
                               'status': 'passed', 'coverage': 'available'}]}}}]}
    initial = CompletionRequestAdmission.input_identity(projected)
    repeated = deepcopy(projected)
    source = repeated['completion_sources'][0]
    source['source_id'] = 'second'
    source['payload']['execution_proof'].update(run_id='r2', call_id='c2')
    source['payload']['named_results']['report_hash'] = 'two'
    source['payload']['named_results']['cases'][0]['case_id'] = 'random2'
    source['payload']['named_results']['cases'].reverse()
    repeated['available_source_ids'] = ['second']
    repeated['completion_sources'].append(deepcopy(source))
    assert CompletionRequestAdmission.input_identity(repeated)['fingerprint'] == initial['fingerprint']
    assert CompletionRequestAdmission.input_identity(repeated)['source_ids'] != initial['source_ids']
    source['payload']['named_results']['cases'][0]['name'] = 'handles missing files'
    assert CompletionRequestAdmission.input_identity(repeated)['fingerprint'] != initial['fingerprint']


def test_same_content_context_publication_is_not_semantic_novelty(tmp_path, monkeypatch):
    async def run():
        from backend.app.desktop.agent_loop.completion_admission import CompletionRequestAdmission
        from backend.app.desktop.context_evolution import ContextRevisionRepository

        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            provider.valid = True
            await _step(runtime, fixture['loop_id'])
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                current = await session.get(LoopRound, fixture['round_id'])
                before = await CompletionRequestAdmission().read(session, loop, current)
                repository = ContextRevisionRepository()
                original = await repository.current(session, fixture['context_id'])
                ref = original.ref.model_copy(update={'revision_id': uuid.uuid4().hex,
                    'generation': original.ref.generation + 1, 'checkpoint_id': uuid.uuid4().hex})
                clone = original.model_copy(update={'ref': ref})
                await repository.insert(session, clone)
                await repository.switch_current(session, ref, expected_ref=original.ref)
            async with sessions() as session:
                after = await CompletionRequestAdmission().read(session, loop, current)
                assert after['input']['fingerprint'] == before['input']['fingerprint']
                assert not after['allowed']
    asyncio.run(run())


def test_legacy_observation_hash_retains_original_field_set():
    from backend.app.desktop.agent_loop.observation import observation_hash
    from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope

    envelope = LoopObservationEnvelope(loop_id='l', round_id='r', loop_revision=1, goal_revision=1,
        authority_revision=1, observed_frontier_hash='f' * 64, grant={}, portfolio_frontier=(), workspace={},
        budget={}, goal={'goal': 'legacy'})
    original = envelope.model_dump(mode='json')
    original.pop('completion_admission')
    original.pop('completion_eligibility')
    expected = hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert observation_hash(envelope) == expected


@pytest.mark.parametrize('legacy_contract', [False, True], ids=['current', 'v1'])
def test_kernel_rejects_equivalent_saved_unknown_without_new_worker(tmp_path, monkeypatch, legacy_contract):
    async def run():
        from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
        from backend.tests.test_round_task_progress import _Checkpointer
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            await _prepare(sessions, fixture, identity)
            assert (await LoopKernel(sessions).commit(_intent(observation))).status == 'committed'
            provider.valid = True
            await _step(runtime, fixture['loop_id'])
            if legacy_contract:
                async with sessions.begin() as session:
                    worker = await session.scalar(select(LoopWorkerRequest).where(LoopWorkerRequest.loop_id == fixture['loop_id']))
                    previous = deepcopy(worker.scope['completion_admission_input'])
                    previous['contract'] = previous['semantic']['contract'] = 'completion-semantic-input-v1'
                    worker.scope = {**worker.scope, 'completion_admission_input': previous}
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                current = await session.get(LoopRound, loop.current_round_id)
            await _publish_memory(sessions, loop.loop_id)
            next_observation = await LoopObservationService(sessions, _Checkpointer()).capture(loop.loop_id, current.round_id)
            before = len(provider.inputs)
            result = await LoopKernel(sessions).commit(_intent(next_observation))
            assert result.status == 'rejected'
            assert 'completion_evidence_unchanged' in result.reason
            assert await runtime.drain(loop.loop_id) == 0
            assert len(provider.inputs) == before
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LoopWorkerRequest).where(LoopWorkerRequest.loop_id == loop.loop_id)) == 1
    asyncio.run(run())


def test_real_same_revision_qualification_and_reexecution_admission(tmp_path, monkeypatch):
    async def run():
        from backend.app.desktop.agent_loop.completion_admission import CompletionRequestAdmission
        from backend.app.desktop.domain_evidence.models import DesktopDomainResult
        from backend.app.desktop.models import DesktopRun, DesktopWorkspace
        from backend.tests.completion_evidence_support import record_verified_fixture

        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            provider.valid = True
            await _step(runtime, fixture['loop_id'])
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                current = await session.get(LoopRound, fixture['round_id'])
                root = (await session.get(DesktopWorkspace, loop.workspace_id)).path
                assert not (await CompletionRequestAdmission().read(session, loop, current))['allowed']
            source_id = await record_verified_fixture(sessions, loop.loop_id, fixture['context_id'], current.round_id, root, 1)
            async with sessions.begin() as session:
                source = await session.get(DesktopDomainResult, source_id)
                owner = await session.get(DesktopRun, source.run_id)
                settled = owner.settled_at
                owner.settled_at = None
            async with sessions() as session:
                assert not (await CompletionRequestAdmission().read(session, loop, current))['allowed']
            async with sessions.begin() as session:
                (await session.get(DesktopRun, owner.run_id)).settled_at = settled
            async with sessions() as session:
                available = await CompletionRequestAdmission().read(session, loop, current)
                assert available['allowed'] and available['input']['source_ids'] == [source_id]
            second_id = await record_verified_fixture(sessions, loop.loop_id, fixture['context_id'], current.round_id, root, 1)
            async with sessions() as session:
                repeated = await CompletionRequestAdmission().read(session, loop, current)
                assert repeated['input']['fingerprint'] == available['input']['fingerprint']
                assert set(repeated['input']['source_ids']) == {source_id, second_id}
    asyncio.run(run())


def test_frozen_patrol_blocker_matches_kernel_policy(tmp_path, monkeypatch):
    async def run():
        from backend.app.desktop.agent_loop.patrol_contract import PatrolDecisionContract
        from backend.app.desktop.agent_loop.patrol import PatrolContractViolation
        from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
        from backend.tests.test_round_task_progress import _Checkpointer

        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            await _prepare(sessions, fixture, identity)
            assert (await LoopKernel(sessions).commit(_intent(observation))).status == 'committed'
            provider.valid = True
            await _step(runtime, fixture['loop_id'])
            await _publish_memory(sessions, fixture['loop_id'])
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
            frozen = await LoopObservationService(sessions, _Checkpointer()).capture(loop.loop_id, loop.current_round_id)
            assert frozen.completion_admission['code'] == 'completion_evidence_unchanged'
            proposal = _intent(frozen)
            with pytest.raises(PatrolContractViolation, match='completion_evidence_unchanged'):
                PatrolDecisionContract().validate(actions=proposal.actions,
                    mission_references=(), observation=frozen)
            from backend.app.desktop.agent_loop.round_orchestration import LoopRoundOrchestrator
            from backend.app.desktop.agent_loop.patrol_session_models import LoopPatrolSession
            from backend.tests.config_helpers import app_config_for, ToolCapableFakeChatModel
            from langchain_core.messages import AIMessage

            response = {'rationale': '重新验收', 'mission_references': [{'role': 'outcome', 'reference_id': 'outcome'},
                        {'role': 'completion_check', 'reference_id': 'tests'}],
                        'actions': [a.model_dump(mode='json') for a in proposal.actions]}
            model = ToolCapableFakeChatModel(scripted=[AIMessage(content=json.dumps(response)) for _ in range(3)])
            monkeypatch.setattr('backend.app.desktop.agent_loop.round_orchestration.create_chat_model', lambda **kwargs: model)
            config = app_config_for('worker-retry', None)
            config.models[0].curation_output_method = 'prompt_json'
            orchestrator = LoopRoundOrchestrator(sessions, config, LoopKernel(sessions), _Checkpointer())
            claim = await LoopCoordinator(sessions).claim('d9-patrol')
            assert claim.round_id == frozen.round_id
            await orchestrator._patrol_sessions.begin(claim)
            with pytest.raises(PatrolContractViolation, match='completion_evidence_unchanged'):
                await orchestrator._decide_patrol(claim, None, frozen.grant['holder_id'], frozen)
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                current = await session.get(LoopRound, frozen.round_id)
                patrol = await session.scalar(select(LoopPatrolSession).where(LoopPatrolSession.round_id == frozen.round_id))
                assert loop.status == 'waiting_user' and current.status == 'error' and patrol.status == 'failed'
                assert patrol.terminal_outcome['reason_code'] == 'completion_evidence_unchanged'
                assert await session.scalar(select(func.count()).select_from(LoopWorkerRequest).where(LoopWorkerRequest.loop_id == loop.loop_id)) == 1
    asyncio.run(run())
