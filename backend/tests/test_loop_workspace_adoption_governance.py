"""本文件对外提供 Workspace adoption 收口的真实 PostgreSQL 回归。

输入为隔离数据库、已授权采用决策、已采用记录及独立暂停事务；输出为统一进展、唯一后继、屏障等待、陈旧身份刷新与迟到回调保护断言。
具体工作流为播种明确的采用阶段事实，调用生产请求/收口端口并控制真实行锁交错，不调用外部 Git applier；
已采用 fixture 只证明事务阶段，不冒充原生 Git 工作区验收。示例：pytest backend/tests/test_loop_workspace_adoption_governance.py。
"""

import asyncio
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
import uuid

import pytest
from sqlalchemy import delete, func, select

from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopAction, LoopBudgetUsage, LoopDecision, LoopDelegationGrant, LoopRound, LoopWorkerRequest
from backend.app.desktop.patrol.agent_loop.workspaces.workspace_adoption import LoopWorkspaceAdoptionService
from backend.app.desktop.workspaces.coordination.models import WorkspaceAdoption, WorkspaceSlot
from backend.tests.test_loop_governance_convergence import _loop, _freeze
from backend.tests.test_loop_termination_lock_order import _race_pause
from backend.tests.test_git_workspace_adoption import _git, _repository
from backend.tests.test_workspace_adoption_recovery import _crash


pytestmark = pytest.mark.usefixtures('runtime_postgres_database')


async def _seed_adoption(sessions, fixture, *, adopted=True):
    decision_id, action_id, source_id = (uuid.uuid4().hex for _ in range(3))
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, fixture['loop_id'])
        current = await _freeze(session, fixture)
        target = await session.scalar(select(WorkspaceSlot).where(
            WorkspaceSlot.workspace_id == loop.workspace_id, WorkspaceSlot.kind == 'authoritative'))
        source = WorkspaceSlot(slot_id=source_id, workspace_id=loop.workspace_id, kind='isolated',
            root_path=target.root_path + '-isolated-fixture', current_fingerprint='b' * 64,
            owner_loop_id=loop.loop_id, revision=2)
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.status == 'active'))
        grant.capabilities = [*grant.capabilities, 'adopt_workspace_result']
        decision = LoopDecision(decision_id=decision_id, loop_id=loop.loop_id, round_id=current.round_id,
            holder_id=loop.holder_id, intent={}, rationale='isolated adoption phase fixture',
            status='adopting', idempotency_key='adoption-test:' + decision_id,
            authority_committed_at=datetime.now(UTC), observation_id=current.observation_id)
        payload = {'action': 'adopt_workspace_result', 'source_slot_id': source_id,
            'source_revision': 2, 'rationale': 'adopt verified isolated fixture'}
        session.add_all([source, decision])
        await session.flush()
        current.decision_id, current.status = decision_id, 'adopting'
        session.add(LoopAction(action_id=action_id, decision_id=decision_id, loop_id=loop.loop_id,
            position=1, action_type='adopt_workspace_result', payload=payload, status='pending', result={}))
        adoption = WorkspaceAdoption(adoption_id=action_id, source_slot_id=source_id,
            target_slot_id=target.slot_id, source_revision=2, expected_target_revision=current.workspace_revision,
            resulting_target_revision=current.workspace_revision + 1 if adopted else None,
            status='adopted' if adopted else 'pending')
        session.add(adoption)
        if adopted:
            target.revision += 1
            target.current_fingerprint = 'b' * 64
        usage = await session.get(LoopBudgetUsage, loop.loop_id)
        usage.no_progress_count = 3
    return decision_id, adoption


async def _finish(sessions, decision_id, adoption):
    async with sessions.begin() as session:
        return await LoopWorkspaceAdoptionService(sessions)._finish(session, decision_id, adoption)


async def _request(sessions, decision_id):
    async with sessions.begin() as session:
        return await LoopWorkspaceAdoptionService(sessions)._request(session, decision_id)


def test_adoption_records_progress_and_creates_one_successor(tmp_path):
    async def run():
        async with _loop(tmp_path, 'adopt-prog') as (sessions, fixture):
            decision_id, adoption = await _seed_adoption(sessions, fixture)
            assert (await _finish(sessions, decision_id, adoption)).status == 'committed'
            assert (await _finish(sessions, decision_id, adoption)).status == 'committed'
            from backend.app.desktop.patrol.agent_loop.task_progress.runtime import TaskProgressRuntime
            from backend.app.desktop.patrol.agent_loop.coordinator import LoopCoordinator
            from backend.tests.test_loop_memory_execution_recovery import ControlledMemory

            memory = ControlledMemory()
            memory.release.set()
            await TaskProgressRuntime(sessions, None, model_factory=lambda name: memory).drain(loop_id=fixture['loop_id'])
            await LoopCoordinator(sessions).maintain_rounds()
            async with sessions() as session:
                usage = await session.get(LoopBudgetUsage, fixture['loop_id'])
                current = await session.get(LoopRound, fixture['round_id'])
                loop = await session.get(AgentLoop, fixture['loop_id'])
                successor = await session.get(LoopRound, loop.current_round_id)
                assert usage.rounds == 1
                assert usage.no_progress_count == 0
                assert current.barrier['progress']['progressed'] is True
                assert successor.number == current.number + 1
                assert successor.workspace_revision == adoption.resulting_target_revision
                assert await session.scalar(select(func.count()).select_from(LoopRound).where(
                    LoopRound.loop_id == loop.loop_id)) == 2
    asyncio.run(run())


def test_adoption_waits_for_unsettled_round_consequences(tmp_path):
    async def run():
        async with _loop(tmp_path, 'adopt-barrier') as (sessions, fixture):
            decision_id, adoption = await _seed_adoption(sessions, fixture)
            async with sessions.begin() as session:
                session.add(LoopWorkerRequest(worker_request_id=uuid.uuid4().hex, loop_id=fixture['loop_id'],
                    round_id=fixture['round_id'], kind='completion_verifier', status='pending', scope={}))
            assert (await _finish(sessions, decision_id, adoption)).status == 'committed'
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                current = await session.get(LoopRound, fixture['round_id'])
                usage = await session.get(LoopBudgetUsage, loop.loop_id)
                assert loop.current_round_id == current.round_id
                assert current.status != 'settled'
                assert current.barrier['consequences']['active_workers'] == 1
                assert usage.rounds == 0
                assert usage.no_progress_count == 3
    asyncio.run(run())


@pytest.mark.parametrize('entry', ('request', 'finish'))
def test_adoption_and_pause_share_first_loop_lock(tmp_path, monkeypatch, entry):
    async def run():
        async with _loop(tmp_path, 'adopt-pause') as (sessions, fixture):
            decision_id, adoption = await _seed_adoption(sessions, fixture, adopted=entry == 'finish')
            operation = partial(_request, sessions, decision_id) if entry == 'request' else partial(_finish, sessions, decision_id, adoption)
            await _race_pause(operation, fixture, monkeypatch)
            async with sessions() as session:
                assert (await session.get(AgentLoop, fixture['loop_id'])).status == 'paused'
    asyncio.run(run())


def test_late_adoption_completion_preserves_pause_terminal(tmp_path):
    async def run():
        async with _loop(tmp_path, 'adopt-late') as (sessions, fixture):
            decision_id, adoption = await _seed_adoption(sessions, fixture)
            await fixture['service'].control(fixture['loop_id'], 'pause')
            result = await _finish(sessions, decision_id, adoption)
            assert result.status == 'superseded'
            async with sessions() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                current = await session.get(LoopRound, fixture['round_id'])
                decision = await session.get(LoopDecision, decision_id)
                action = await session.get(LoopAction, adoption.adoption_id)
                assert loop.status == 'paused'
                assert current.status == 'superseded'
                assert decision.status == 'superseded'
                assert decision.rejection == {'reason': 'loop_paused'}
                assert action.status != 'applied'
                assert loop.current_round_id == current.round_id
    asyncio.run(run())


def test_adoption_refreshes_cached_owner_after_pause(tmp_path):
    async def run():
        async with _loop(tmp_path, 'adopt-cache') as (sessions, fixture):
            decision_id, adoption = await _seed_adoption(sessions, fixture)
            async with sessions.begin() as session:
                loop = await session.get(AgentLoop, fixture['loop_id'])
                current = await session.get(LoopRound, fixture['round_id'])
                decision = await session.get(LoopDecision, decision_id)
                action = await session.get(LoopAction, adoption.adoption_id)
                assert loop.status == 'running' and decision.status == 'adopting'
                await fixture['service'].control(fixture['loop_id'], 'pause')
                result = await LoopWorkspaceAdoptionService(sessions)._finish(session, decision_id, adoption)
                assert result.status == 'superseded'
                assert loop.status == 'paused' and current.status == 'superseded'
                assert decision.rejection == {'reason': 'loop_paused'}
                assert action.status != 'applied'
                assert loop.current_round_id == current.round_id
    asyncio.run(run())


@pytest.mark.parametrize("phase", ("before_apply", "after_apply", "after_commit"))
@pytest.mark.parametrize("paused", (False, True))
def test_startup_recovers_real_git_adoption_and_respects_pause(tmp_path, phase, paused):
    from backend.app.desktop.patrol.agent_loop.coordinator import LoopCoordinator
    from backend.app.desktop.patrol.agent_loop.recovery.service import AgentLoopRecovery
    from backend.app.desktop.runs.orchestration import RunOutboxConsumer
    from backend.app.desktop.workspaces.coordination import WorkspaceAdoptionRequest, WorkspaceFingerprinter

    async def run():
        async with _loop(tmp_path, 'adopt-restart') as (sessions, fixture):
            decision_id, pending = await _seed_adoption(sessions, fixture, adopted=False)
            async with sessions.begin() as session:
                source = await session.get(WorkspaceSlot, pending.source_slot_id)
                target = await session.get(WorkspaceSlot, pending.target_slot_id)
                target_root, source_root = Path(target.root_path), Path(source.root_path)
                baseline = _repository(target_root)
                _git(target_root, 'worktree', 'add', '--detach', str(source_root), baseline)
                (source_root / 'tracked.txt').write_text('recovered loop result\n', encoding='utf-8')
                source.base_revision = target.base_revision = baseline
                source.current_fingerprint = WorkspaceFingerprinter().capture(source_root).digest
                target.current_fingerprint = WorkspaceFingerprinter().capture(target_root).digest
                before = target.current_fingerprint
                await session.execute(delete(WorkspaceAdoption).where(WorkspaceAdoption.adoption_id == pending.adoption_id))
            request = WorkspaceAdoptionRequest(adoption_id=pending.adoption_id,
                source_slot_id=pending.source_slot_id, target_slot_id=pending.target_slot_id,
                source_revision=pending.source_revision, expected_target_revision=pending.expected_target_revision)
            await _crash(request, target_root, phase)
            if paused:
                await fixture['service'].control(fixture['loop_id'], 'pause')
            recovery = AgentLoopRecovery(sessions, LoopCoordinator(sessions), RunOutboxConsumer(sessions))
            await recovery.reconcile()
            await recovery.reconcile()
            async with sessions() as session:
                adoption = await session.get(WorkspaceAdoption, pending.adoption_id)
                target = await session.get(WorkspaceSlot, pending.target_slot_id)
                decision = await session.get(LoopDecision, decision_id)
                action = await session.get(LoopAction, pending.adoption_id)
                loop = await session.get(AgentLoop, fixture['loop_id'])
                if paused and phase == 'before_apply':
                    assert adoption.status == 'rejected'
                    assert target.current_fingerprint == before
                    assert target.revision == pending.expected_target_revision
                else:
                    assert adoption.status == 'adopted'
                    assert target.revision == pending.expected_target_revision + 1
                    assert target.current_fingerprint == WorkspaceFingerprinter().capture(target_root).digest
                    assert (target_root / 'tracked.txt').read_text(encoding='utf-8') == 'recovered loop result\n'
                if paused:
                    assert loop.status == 'paused'
                    assert decision.status == 'superseded'
                    assert action.status != 'applied'
                else:
                    assert decision.status == 'committed'
                    assert action.status == 'applied'
    asyncio.run(run())
