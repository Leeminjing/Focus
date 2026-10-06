"""本文件对外提供 D11 正文读面、历史身份和实际请求容量的 PostgreSQL 回归。

输入为隔离 Loop、可信文件工具审计及当前文本；输出为完整脱敏正文、严格拒绝、旧 hash 保留及零 Provider 断言。
具体工作流为生产 Recorder 记录不可变 Artifact，Catalog 派生正文，再走真实 Worker 容量准入和共享语义比较。
夹具仅构造已完成工具审计；真实文件工具链另由 test_loop_artifact_source_contract 覆盖。
示例：pytest backend/tests/test_completion_artifact_content.py；所有文件只写入临时工作区。
"""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import uuid

from langchain_core.messages import ToolMessage
import pytest
from sqlalchemy import select, text

from backend.app.desktop.agent_loop.completion_admission import CompletionRequestAdmission
from backend.app.desktop.agent_loop.completion_evidence_catalog import CompletionEvidenceCatalog
from backend.app.desktop.agent_loop.completion_sources import CompletionSourceRejection
from backend.app.desktop.agent_loop.models import AgentLoop, LoopRound, LoopWorkerRequest
from backend.app.desktop.domain_evidence.artifacts import ArtifactObservationRecorder
from backend.app.desktop.domain_evidence.identity import canonical_hash
from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.models import DesktopRun, DesktopWorkspace, ToolExecutionAttempt
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from backend.tests.test_loop_worker_recovery import _case, _step
from focus.history import content_hash, serialize_history_message

pytestmark = pytest.mark.usefixtures('isolated_postgres_database')


async def _artifact(sessions, fixture, body, tool='read_file'):
    run_id, call_id, attempt_id = (uuid.uuid4().hex for _ in range(3))
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, fixture['loop_id'])
        root = Path((await session.get(DesktopWorkspace, loop.workspace_id)).path)
        slot = await session.scalar(select(WorkspaceSlot).where(
            WorkspaceSlot.workspace_id == loop.workspace_id, WorkspaceSlot.kind == 'authoritative'))
        path = root / 'review.md'
        path.write_bytes(body.encode('utf-8'))
        args = {'path': str(path)} | ({'content': body} if tool == 'write_file' else {})
        call = {'id': call_id, 'name': tool, 'args': args, 'type': 'tool_call'}
        run = DesktopRun(run_id=run_id, task_id=fixture['context_id'], agent_id='fixture:' + run_id,
            kind='worker', status='success', loop_id=fixture['loop_id'], round_id=fixture['round_id'],
            equipment={'permissions': ['read', 'write']}, workspace_anchor={'slot_id': slot.slot_id},
            workspace_result={'revision': 1, 'workspace_status': 'settled'}, settled_at=datetime.now(UTC))
        session.add(run)
        await session.flush()
        output = body if tool == 'read_file' else f'已写入真实宿主机路径: {path}'
        session.add(ToolExecutionAttempt(attempt_id=attempt_id, run_id=run_id, call_id=call_id,
            call_hash=content_hash(call), status='completed', result=serialize_history_message(
                ToolMessage(content=output, name=tool, tool_call_id=call_id))))
        await session.flush()
        await ArtifactObservationRecorder().record(session, run, {call_id: call})
        source = await session.scalar(select(DesktopDomainResult).where(
            DesktopDomainResult.run_id == run_id, DesktopDomainResult.kind == 'artifact'))
        assert source is not None
        frozen = {'mission': {'revision': 1, 'completion_checks': []}, 'workspace': {'revision': 1},
            'frontier_hash': 'f', 'completion_sources': [{'source_id': source.result_key,
                'kind': 'artifact', 'run_id': run_id, 'payload': deepcopy(source.payload)}]}
        return path, source.result_key, frozen


@pytest.mark.parametrize('tool', ['read_file', 'write_file'])
def test_current_body_is_complete_redacted_and_does_not_mutate_history(tmp_path, monkeypatch, tool):
    async def run():
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, *unused):
            body = '正式独立结论\n' + '检查依据\n' * 10000 + 'secret-content-value\n尾部判断'
            path, key, frozen = await _artifact(sessions, fixture, body, tool)
            original = deepcopy(frozen)
            async with sessions() as session:
                await session.execute(text('SET TRANSACTION READ ONLY'))
                view = await CompletionEvidenceCatalog().project(session, frozen, fixture['loop_id'], 1,
                    secrets=('secret-content-value',))
                projected = view['completion_sources'][0]['artifact_content']
                assert projected['text'] == body.replace('secret-content-value', '***')
                assert projected['sha256'] == sha256(path.read_bytes()).hexdigest()
                assert projected['source_result_key'] == key and projected['complete'] is True
                assert view['available_source_ids'] == [key]
                assert frozen == original and canonical_hash(frozen) == canonical_hash(original)
                assert (await session.get(DesktopDomainResult, key)).payload == original['completion_sources'][0]['payload']
                assert not session.dirty and 'secret-content-value' not in json.dumps(view)
    asyncio.run(run())


def test_body_is_new_semantic_information_but_random_identity_is_not():
    frozen = {'mission': {}, 'workspace': {}, 'completion_sources': [{'kind': 'artifact', 'source_id': 'a',
        'eligibility': {'status': 'available', 'code': None}, 'payload': {'path': 'review.md', 'sha256': 'hash'}}]}
    old = CompletionRequestAdmission.input_identity(frozen)
    projected = deepcopy(frozen)
    projected['completion_sources'][0]['artifact_content'] = {'contract': 'trusted-artifact-content-v1',
        'complete': True, 'encoding': 'utf-8', 'sha256': 'hash', 'text': 'actual body', 'source_result_key': 'a'}
    current = CompletionRequestAdmission.input_identity(projected)
    assert current['fingerprint'] != old['fingerprint']
    duplicate = deepcopy(projected)
    duplicate['completion_sources'][0]['source_id'] = 'b'
    duplicate['completion_sources'][0]['artifact_content']['source_result_key'] = 'b'
    assert CompletionRequestAdmission.input_identity(duplicate)['fingerprint'] == current['fingerprint']


@pytest.mark.parametrize('change', ['changed', 'binary', 'invalid_utf8', 'missing', 'outside', 'run',
    'bound', 'tool', 'result_failed', 'result_call', 'partial_read', 'revision', 'missing_proof', 'size', 'unreadable',
    'human_result', 'wrong_workspace', 'attempt_hash', 'wrong_write_path'])
def test_invalid_body_is_not_available(tmp_path, monkeypatch, change):
    async def run():
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, *unused):
            path, key, frozen = await _artifact(sessions, fixture, 'trusted full body',
                'write_file' if change == 'wrong_write_path' else 'read_file')
            async with sessions.begin() as session:
                source = await session.get(DesktopDomainResult, key)
                payload = deepcopy(source.payload)
                proof = payload['execution_proof']
                attempt = await session.get(ToolExecutionAttempt, proof['attempt_id'])
                if change in {'changed', 'binary', 'invalid_utf8'}:
                    path.write_bytes({'changed': b'changed', 'binary': b'full\x00binary', 'invalid_utf8': b'bad\xff'}[change])
                    if change != 'changed':
                        payload.update(sha256=sha256(path.read_bytes()).hexdigest(), size_bytes=path.stat().st_size)
                elif change == 'missing':
                    path.unlink()
                elif change == 'outside':
                    payload['path'] = '../review.md'
                elif change in {'run', 'bound', 'tool'}:
                    proof[{'run': 'run_id', 'bound': 'bound', 'tool': 'tool_name'}[change]] = {'run': 'other', 'bound': False, 'tool': 'shell'}[change]
                elif change in {'result_failed', 'result_call', 'partial_read'}:
                    attempt.result = serialize_history_message(ToolMessage(content='partial' if change == 'partial_read' else 'trusted full body',
                        name='read_file', tool_call_id='other' if change == 'result_call' else proof['call_id'],
                        status='error' if change == 'result_failed' else 'success'))
                elif change == 'revision':
                    payload['workspace_revision'] = 0
                elif change == 'missing_proof':
                    payload.pop('execution_proof')
                elif change == 'size':
                    payload['size_bytes'] = 1
                elif change == 'human_result':
                    from langchain_core.messages import HumanMessage
                    attempt.result = serialize_history_message(HumanMessage(content='trusted full body'))
                elif change == 'wrong_workspace':
                    proof['workspace'] = str(tmp_path)
                elif change == 'attempt_hash':
                    proof['call_hash'] = '0' * 64
                elif change == 'wrong_write_path':
                    attempt.result = serialize_history_message(ToolMessage(content='已写入真实宿主机路径: other.md',
                        name='write_file', tool_call_id=attempt.call_id))
                else:
                    original = Path.read_bytes
                    def denied(target):
                        if target == path:
                            raise PermissionError('private path must not leak')
                        return original(target)
                    monkeypatch.setattr(Path, 'read_bytes', denied)
                source.payload = payload
                frozen['completion_sources'][0]['payload'] = deepcopy(payload)
            async with sessions() as session:
                view = await CompletionEvidenceCatalog().project(session, frozen, fixture['loop_id'], 1)
                entry = view['completion_sources'][0]
                assert view['available_source_ids'] == []
                assert entry['eligibility']['status'] in {'stale', 'unsettled', 'unsupported'}
                assert 'artifact_content' not in entry
                assert 'private path must not leak' not in json.dumps(view)
    asyncio.run(run())


def test_full_artifact_capacity_rejects_before_provider_or_reservation(tmp_path, monkeypatch):
    async def run():
        from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            await _artifact(sessions, fixture, '甲' * 100000)
            runtime._app_config.models[0].context_window = 16384
            runtime._app_config.models[0].curation_max_output_tokens = 512
            from backend.app.desktop.agent_loop.completion import CompletionCandidateValidator
            original_admit = CompletionCandidateValidator.admit
            baseline_checks = []
            async def guard(candidate, model, schema, system, payload):
                metadata = deepcopy(payload)
                for source in metadata.get('completion_sources', ()):
                    source.pop('artifact_content', None)
                await original_admit(candidate, model, schema, system, metadata)
                baseline_checks.append(True)
                await original_admit(candidate, model, schema, system, payload)
            monkeypatch.setattr(CompletionCandidateValidator, 'admit', guard)
            await _step(runtime, fixture['loop_id'])
            assert baseline_checks and provider.inputs == []
            async with sessions() as session:
                assert not tuple(await session.scalars(select(LoopIndexBudgetReservation).where(
                    LoopIndexBudgetReservation.loop_id == fixture['loop_id'])))
                worker = await session.get(LoopWorkerRequest, identity)
                assert 'provider_request_window' in json.dumps(worker.result)
    asyncio.run(run())


@pytest.mark.parametrize('legacy', ['completion-semantic-input-v1', 'completion-semantic-input-v2', None])
def test_legacy_unknown_does_not_retroactively_gain_body_and_new_unknown_is_deduplicated(tmp_path, monkeypatch, legacy):
    async def run():
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            path, key, original = await _artifact(sessions, fixture, '正式独立结论')
            provider.valid = True
            await _step(runtime, fixture['loop_id'])
            async with sessions.begin() as session:
                worker = await session.get(LoopWorkerRequest, identity)
                current_identity = deepcopy(worker.scope['completion_admission_input'])
                old_frozen_hash = canonical_hash(worker.scope['frozen_worker_input'])
                metadata = await CompletionEvidenceCatalog().project(session, worker.scope['frozen_worker_input'],
                    fixture['loop_id'], 1, include_artifact_content=False)
                prior = CompletionRequestAdmission.input_identity(metadata,
                    frontier_hash=current_identity['semantic']['frontier_hash'])
                if legacy:
                    prior['contract'] = prior['semantic']['contract'] = legacy
                    prior['fingerprint'] = canonical_hash(prior['semantic'])
                worker.scope = {**worker.scope, 'completion_admission_input': prior if legacy else None}
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                round_row = await session.get(LoopRound, loop.current_round_id)
                admission = await CompletionRequestAdmission().read(session, loop, round_row)
                assert admission['allowed']
                assert canonical_hash((await session.get(LoopWorkerRequest, identity)).scope['frozen_worker_input']) == old_frozen_hash
            async with sessions.begin() as session:
                worker = await session.get(LoopWorkerRequest, identity)
                worker.scope = {**worker.scope, 'completion_admission_input': current_identity}
            async with sessions() as session:
                repeated = await CompletionRequestAdmission().read(session, loop, round_row)
                assert not repeated['allowed'] and repeated['code'] == 'completion_evidence_unchanged'
                assert len(provider.inputs) == 1
    asyncio.run(run())


@pytest.mark.parametrize('change', ['content', 'proof'])
def test_new_body_candidate_and_final_record_recheck_actual_source(tmp_path, monkeypatch, change):
    async def run():
        from backend.app.desktop.agent_loop.completion import CompletionCandidateValidator, CompletionEvidenceService
        from backend.app.desktop.agent_loop.schemas import CompletionVerificationContract, CompletionVerificationResult
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            path, key, frozen = await _artifact(sessions, fixture, 'trusted full body')
            request = (await runtime._claim_many(1, fixture['loop_id']))[0]
            payload, loop, round_row = await runtime._evidence(request)
            binding = dict(verification_id=uuid.uuid4().hex, loop_id=loop.loop_id, round_id=round_row.round_id,
                goal_revision=round_row.goal_revision, frontier_hash=round_row.frontier_hash, workspace_revision=1)
            checks = [{'check_id': 'files', 'expected_evidence_kinds': ['artifact']}]
            proposal = CompletionVerificationResult.model_validate({'criteria': [{'check_id': 'files', 'status': 'satisfied',
                'explanation': 'actual body', 'evidence': [{'kind': 'artifact', 'source_id': key}]}], 'conclusion': 'satisfied'})
            candidate = CompletionCandidateValidator(sessions, request, binding, checks, frozen['completion_sources'],
                require_artifact_content=True)
            await candidate.validate(proposal)
            async with sessions.begin() as session:
                from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
                mission = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == loop.loop_id))
                mission.completion_checks = [{'check_id': 'files', 'claim': 'actual body', 'required': True,
                    'expected_evidence_kinds': ['artifact']}]
                worker = await session.get(LoopWorkerRequest, identity)
                worker.scope = {**worker.scope, 'completion_input_views': [{'contract': 'completion-current-sources-v2'}]}
                if change == 'content':
                    path.write_text('changed after candidate', encoding='utf-8')
                else:
                    source = await session.get(DesktopDomainResult, key)
                    attempt = await session.get(ToolExecutionAttempt, source.payload['execution_proof']['attempt_id'])
                    attempt.result = serialize_history_message(ToolMessage(content='trusted full body',
                        name='read_file', tool_call_id=attempt.call_id, status='error'))
            with pytest.raises(ValueError):
                await candidate.validate(proposal)
            contract = CompletionVerificationContract(**binding, **proposal.model_dump(mode='json'))
            with pytest.raises(CompletionSourceRejection) as rejected:
                await CompletionEvidenceService(sessions).record(contract, identity, retry_identity=request.retry_identity)
            assert rejected.value.code == ('source_stale' if change == 'content' else 'source_unsupported')
    asyncio.run(run())


def test_real_worker_receives_redacted_body_and_records_independent_artifact_result(tmp_path, monkeypatch):
    async def run():
        from langchain_core.messages import AIMessage
        from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
        from backend.app.desktop.agent_loop.models import CompletionVerification
        async with _case(tmp_path, monkeypatch) as (sessions, fixture, runtime, provider, identity, observation):
            secret = 'artifact-key-private-value'
            body = '正式独立 Context 结论\ncan_start: 条件一成立，条件二成立\n' + secret
            path, key, frozen = await _artifact(sessions, fixture, body)
            runtime._app_config.models[0].api_key = secret
            async with sessions.begin() as session:
                mission = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == fixture['loop_id']))
                mission.completion_checks = [{'check_id': 'files', 'claim': '独立结论及两项条件', 'required': True,
                    'expected_evidence_kinds': ['artifact']}]
            async def respond(messages, config):
                provider.inputs.append(messages[-1].content)
                assert secret not in messages[-1].content
                payload = json.loads(messages[-1].content.split('<worker_input>', 1)[1].split('</worker_input>', 1)[0])
                source = next(item for item in payload['completion_sources'] if item['source_id'] == key)
                assert source['artifact_content']['text'] == body.replace(secret, '***')
                config['callbacks'][0].usage_metadata['body-verifier'] = {'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30}
                return AIMessage(content=json.dumps({'criteria': [{'check_id': 'files', 'status': 'satisfied',
                    'explanation': '逐项核对当前正文', 'evidence': [{'kind': 'artifact', 'source_id': key}]}], 'conclusion': 'satisfied'}))
            provider.ainvoke = respond
            await _step(runtime, fixture['loop_id'])
            assert len(provider.inputs) == 1
            async with sessions() as session:
                verification = await session.scalar(select(CompletionVerification).where(
                    CompletionVerification.worker_request_id == identity))
                assert verification is not None and verification.conclusion == 'satisfied'
                worker = await session.get(LoopWorkerRequest, identity)
                assert 'artifact_content' not in json.dumps(worker.scope['frozen_worker_input'])
                assert worker.scope['completion_input_views'][0]['contract'] == 'completion-current-sources-v2'
    asyncio.run(run())
