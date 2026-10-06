"""本文件对外提供人工访问审批与Loop等待同事务收口的回归。
输入为隔离PostgreSQL、真实审批投影和人类恢复事实；输出为严格阻断、来源核对、幂等或全量回滚断言。
具体工作流为仅替换检查点读取，不调用Provider、执行浏览器或更改原审批授权；旧payload和既有Decision保持。
示例：pytest backend/tests/test_loop_user_access_gate_recovery.py -q。
"""

import asyncio
from datetime import UTC,datetime,timedelta
from copy import deepcopy
from types import SimpleNamespace
import os
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import func,select
from sqlalchemy.ext.asyncio import async_sessionmaker,create_async_engine

from backend.app.desktop.agent_loop.gates import PendingDecisionProjector
from backend.app.desktop.agent_loop.models import AgentLoop,LoopPendingDecision,LoopDecision,LoopDirective,LoopEventOutbox,LoopRound
from backend.app.desktop.agent_loop.schemas import LoopWaitResponseRequest
from backend.app.desktop.agent_loop.wait_models import LoopWaitRequest,LoopWaitResponse
from backend.app.desktop.agent_loop.user_access_gate_recovery import UserAccessGateRecovery
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.models import DesktopRun,DesktopWorkspace
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor,WorkspaceLease,WorkspaceSlot
from backend.tests.test_agent_loop_round_liveness import _seed_loop,_stop

pytestmark=pytest.mark.usefixtures('runtime_postgres_database')


async def _gate(sessions,fixture):
    return await PendingDecisionProjector(sessions).project(fixture['loop_id'],{
        'type':'access_approval','recovery':{'status':'resumable','request':{
            'type':'access_review','tool':'browser_run_code_unsafe','agent_role':'main',
            'access_mode':'workspace-write','cwd':'unused'}}})


def test_retry_cannot_claim_human_gate_resolved_without_a_resolution_reader(tmp_path):
    async def run():
        engine=create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        fixture=await _seed_loop(sessions,tmp_path,label='access-gate',started_at=datetime.now(UTC))
        service=fixture['service']
        try:
            gate=await _gate(sessions,fixture)
            wait=await service.active_wait_request(fixture['loop_id'])
            body=LoopWaitResponseRequest(request_revision=wait['revision'],idempotency_key='user-gate-'+uuid.uuid4().hex,answer={'action':'retry'})
            with pytest.raises(HTTPException) as refused:
                await service.resolve_wait_request(fixture['loop_id'],wait['request_id'],body,actor_id='user')
            assert refused.value.status_code==409
            async with sessions() as session:
                assert (await session.get(AgentLoop,fixture['loop_id'])).status=='waiting_user'
                assert (await session.get(LoopPendingDecision,gate.pending_decision_id)).status=='pending'
                assert (await session.get(LoopWaitRequest,wait['request_id'])).status=='open'
                assert await session.scalar(select(func.count()).select_from(LoopWaitResponse).where(LoopWaitResponse.request_id==wait['request_id']))==0
        finally:
            await _stop(service,fixture['loop_id'])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('wrong_lease',[False,True])
def test_authoritative_slot_anchor_is_checked_by_actual_run_and_fence(tmp_path,wrong_lease):
    async def run():
        engine=create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        fixture=await _seed_loop(sessions,tmp_path,label='gate',started_at=datetime.now(UTC))
        try:
            gate,original,resumed,wait,body,_=await _handled(sessions,fixture)
            async with sessions.begin() as session:
                slot=await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id==fixture['snapshot']['workspace_id'],WorkspaceSlot.kind=='authoritative'))
                lease_id=uuid.uuid4().hex
                session.add(WorkspaceLease(lease_id=lease_id,slot_id=slot.slot_id,run_id=resumed.run_id if wrong_lease else original.run_id,
                    mode='write',fencing_token=7,status='released',expires_at=datetime.now(UTC),released_at=datetime.now(UTC)))
                await session.flush()
                session.add(RunExecutionAnchor(run_id=original.run_id,slot_id=slot.slot_id,lease_id=lease_id,
                    context_revision_id=original.context_revision_id,checkpoint_id=original.context_checkpoint_id,
                    observed_workspace_revision=1,observed_fingerprint='a'*64,settled_at=datetime.now(UTC)))
                stored=await session.get(DesktopRun,original.run_id)
                stored.workspace_anchor={'slot_id':slot.slot_id,'lease_id':lease_id,'fencing_token':7}
            if wrong_lease:
                with pytest.raises(HTTPException) as refused:
                    await fixture['service'].resolve_wait_request(fixture['loop_id'],wait['request_id'],body,actor_id='user')
                assert refused.value.status_code==409
            else:
                result=await fixture['service'].resolve_wait_request(fixture['loop_id'],wait['request_id'],body,actor_id='user')
                assert result['created']
            async with sessions() as session:
                assert (await session.get(LoopPendingDecision,gate.pending_decision_id)).status==('pending' if wrong_lease else 'resolved')
        finally:
            await _stop(fixture['service'],fixture['loop_id'])
            await engine.dispose()
    asyncio.run(run())


def test_identical_access_text_on_new_source_run_is_a_different_gate(tmp_path):
    async def run():
        engine=create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        fixture=await _seed_loop(sessions,tmp_path,label='gate',started_at=datetime.now(UTC))
        try:
            gate,_,resumed,_,_,_=await _handled(sessions,fixture)
            newer={**deepcopy(gate.payload),'source':{'task_id':fixture['context_id'],'run_id':resumed.run_id}}
            projected=await PendingDecisionProjector(sessions).project(fixture['loop_id'],newer)
            replay=await PendingDecisionProjector(sessions).project(fixture['loop_id'],newer)
            assert projected.pending_decision_id!=gate.pending_decision_id and projected.pending_decision_id==replay.pending_decision_id
            async with sessions() as session:
                assert (await session.get(LoopPendingDecision,gate.pending_decision_id)).payload==gate.payload
                assert not projected.delegable
        finally:
            await _stop(fixture['service'],fixture['loop_id'])
            await engine.dispose()
    asyncio.run(run())


async def _handled(sessions,fixture,*,legacy=False):
    async with sessions.begin() as session:
        workspace=await session.get(DesktopWorkspace,fixture['snapshot']['workspace_id'])
        original=DesktopRun(run_id=uuid.uuid4().hex,task_id=fixture['context_id'],agent_id=f"main:{fixture['context_id']}",
            kind='main',status='interrupted',origin='delegated_patrol',loop_id=fixture['loop_id'],round_id=fixture['round_id'],
            execution_thread_id='bound-'+fixture['context_id'],checkpoint_ns='main/branch',context_revision_id=fixture['revision_id'],
            context_checkpoint_id='original-checkpoint',equipment={'access_mode':'workspace-write'},
            workspace_anchor={'workspace_path':workspace.path},created_at=datetime.now(UTC),settled_at=datetime.now(UTC))
        session.add(original)
    payload={'type':'access_approval','recovery':{'status':'resumable','request':{
        'type':'access_review','tool':'browser_run_code_unsafe','agent_role':'main','access_mode':'workspace-write','cwd':workspace.path}}}
    if not legacy:
        payload['source']={'task_id':fixture['context_id'],'run_id':original.run_id}
    gate=await PendingDecisionProjector(sessions).project(fixture['loop_id'],payload)
    async with sessions.begin() as session:
        stored=await session.get(LoopPendingDecision,gate.pending_decision_id)
        resumed=DesktopRun(run_id=uuid.uuid4().hex,task_id=original.task_id,agent_id=original.agent_id,kind='main',origin='resume',
            status='success',execution_thread_id=original.execution_thread_id,checkpoint_ns=original.checkpoint_ns,
            context_revision_id=original.context_revision_id,context_checkpoint_id=original.context_checkpoint_id,
            created_at=stored.created_at+timedelta(seconds=1),settled_at=stored.created_at+timedelta(seconds=2),
            workspace_result={'context_publication':'superseded'})
        session.add(resumed)
        if legacy:
            decision_id,action_id=uuid.uuid4().hex,uuid.uuid4().hex
            session.add(LoopDecision(decision_id=decision_id,loop_id=fixture['loop_id'],round_id=fixture['round_id'],holder_id='test',
                intent={'actions':[]},rationale='fixture committed decision',status='committed',idempotency_key=decision_id))
            await session.flush()
            from backend.app.desktop.agent_loop.models import LoopAction
            session.add(LoopAction(action_id=action_id,decision_id=decision_id,loop_id=fixture['loop_id'],position=0,
                action_type='continue_context',payload={},status='pending'))
            await session.flush()
            session.add(LoopDirective(directive_id=uuid.uuid4().hex,loop_id=fixture['loop_id'],round_id=fixture['round_id'],
                decision_id=decision_id,action_id=action_id,target_context_id=original.task_id,target_context_revision_id=original.context_revision_id,
                message_id=uuid.uuid4().hex,content='fixture',content_hash='a'*64,actor_id='test',grant_id='fixture-grant',grant_revision=1,
                goal_revision=1,status='created',lifecycle_state='authorized',correlation_id='test',idempotency_key=uuid.uuid4().hex,
                queued_reason='launch_retry:409:access_review_pending'))
        round_row=await session.get(LoopRound,fixture['round_id'])
        round_row.status='ready'
    asked=[]

    class Checkpointer:
        async def aget_tuple(self,config):
            asked.append(deepcopy(config))
            return SimpleNamespace(config=deepcopy(config),checkpoint={'id':'resolved-checkpoint'},pending_writes=[])

    fixture['service']._user_gate_recovery=UserAccessGateRecovery(Checkpointer())
    wait=await fixture['service'].active_wait_request(fixture['loop_id'])
    body=LoopWaitResponseRequest(request_revision=wait['revision'],idempotency_key='resolved-'+uuid.uuid4().hex,answer={'action':'retry'})
    return gate,original,resumed,wait,body,asked


@pytest.mark.parametrize('legacy',[False,True])
def test_consumed_human_interrupt_resolves_with_wait_atomically_and_idempotently(tmp_path,legacy):
    async def run():
        engine=create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        fixture=await _seed_loop(sessions,tmp_path,label='gate',started_at=datetime.now(UTC))
        try:
            gate,original,resumed,wait,body,asked=await _handled(sessions,fixture,legacy=legacy)
            first=await fixture['service'].resolve_wait_request(fixture['loop_id'],wait['request_id'],body,actor_id='user')
            second=await fixture['service'].resolve_wait_request(fixture['loop_id'],wait['request_id'],body,actor_id='user')
            assert first['created'] and not second['created'] and first['response_id']==second['response_id']
            assert len(asked)==1 and asked[0]['configurable']=={'thread_id':original.execution_thread_id,'checkpoint_ns':original.checkpoint_ns}
            async with sessions() as session:
                stored=await session.get(LoopPendingDecision,gate.pending_decision_id)
                assert stored.status=='resolved' and not stored.delegable and stored.payload==gate.payload
                assert (await session.get(AgentLoop,fixture['loop_id'])).current_round_id==fixture['round_id']
                assert (await session.get(LoopWaitRequest,wait['request_id'])).status=='resolved'
                events=tuple(await session.scalars(select(LoopEventOutbox).where(LoopEventOutbox.loop_id==fixture['loop_id'],
                    LoopEventOutbox.event_type=='LoopPendingDecisionSourceResolved')))
                assert len(events)==1 and events[0].payload['resume_run_id']==resumed.run_id
                assert events[0].payload['source_run_id']==original.run_id and not events[0].payload['access_approved_by_recovery']
                assert (await session.get(DesktopRun,resumed.run_id)).workspace_result=={'context_publication':'superseded'}
                if legacy:
                    decisions=tuple(await session.scalars(select(LoopDecision).where(LoopDecision.loop_id==fixture['loop_id'])))
                    assert len(decisions)==1 and decisions[0].status=='committed'
        finally:
            await _stop(fixture['service'],fixture['loop_id'])
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['still_pending','missing','unreadable','wrong_checkpoint','wrong_execution','wrong_directory','wrong_mode','unsettled','unknown_source'])
def test_unproven_user_resolution_rolls_back_response_and_keeps_gate(tmp_path,failure):
    async def run():
        engine=create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        fixture=await _seed_loop(sessions,tmp_path,label='gate',started_at=datetime.now(UTC))
        try:
            gate,original,resumed,wait,body,_=await _handled(sessions,fixture)
            async with sessions.begin() as session:
                if failure=='wrong_execution':
                    (await session.get(DesktopRun,resumed.run_id)).execution_thread_id='foreign-thread'
                elif failure=='wrong_directory':
                    (await session.get(DesktopRun,original.run_id)).workspace_anchor={'workspace_path':str(tmp_path/'foreign')}
                elif failure=='wrong_mode':
                    (await session.get(DesktopRun,original.run_id)).equipment={'access_mode':'danger-full-access'}
                elif failure=='unsettled':
                    (await session.get(DesktopRun,resumed.run_id)).settled_at=None
                elif failure=='unknown_source':
                    stored=await session.get(LoopPendingDecision,gate.pending_decision_id)
                    stored.payload={**stored.payload,'source':{'task_id':original.task_id,'run_id':'foreign-run'}}

            class Checkpointer:
                async def aget_tuple(self,config):
                    if failure=='unreadable':
                        raise OSError('transport source unavailable')
                    if failure=='missing':
                        return None
                    writes=[('task','__interrupt__',{'type':'access_review','tool':'browser_run_code_unsafe'})] if failure=='still_pending' else []
                    actual=deepcopy(config)
                    if failure=='wrong_checkpoint':
                        actual['configurable']['thread_id']='foreign-thread'
                    return SimpleNamespace(config=actual,checkpoint={'id':'checked'},pending_writes=writes)

            fixture['service']._user_gate_recovery=UserAccessGateRecovery(Checkpointer())
            with pytest.raises(HTTPException) as refused:
                await fixture['service'].resolve_wait_request(fixture['loop_id'],wait['request_id'],body,actor_id='user')
            assert refused.value.status_code==409 and refused.value.detail['code']=='user_gate_not_resolved'
            async with sessions() as session:
                assert (await session.get(AgentLoop,fixture['loop_id'])).status=='waiting_user'
                assert (await session.get(LoopPendingDecision,gate.pending_decision_id)).status=='pending'
                assert (await session.get(LoopWaitRequest,wait['request_id'])).status=='open'
                assert await session.scalar(select(func.count()).select_from(LoopWaitResponse).where(LoopWaitResponse.request_id==wait['request_id']))==0
                assert await session.scalar(select(func.count()).select_from(LoopEventOutbox).where(LoopEventOutbox.loop_id==fixture['loop_id'],
                    LoopEventOutbox.event_type=='LoopPendingDecisionSourceResolved'))==0
        finally:
            await _stop(fixture['service'],fixture['loop_id'])
            await engine.dispose()
    asyncio.run(run())


def test_later_transition_failure_rolls_back_gate_and_user_response(tmp_path,monkeypatch):
    async def run():
        engine=create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        fixture=await _seed_loop(sessions,tmp_path,label='gate',started_at=datetime.now(UTC))
        try:
            gate,_,_,wait,body,_=await _handled(sessions,fixture)
            append=LoopEventJournal.append

            async def fail(journal,session,loop_id,draft):
                if draft.kind=='loop.wait.resumed':
                    raise RuntimeError('Injected transaction failure')
                return await append(journal,session,loop_id,draft)

            monkeypatch.setattr(LoopEventJournal,'append',fail)
            with pytest.raises(RuntimeError,match='Injected transaction failure'):
                await fixture['service'].resolve_wait_request(fixture['loop_id'],wait['request_id'],body,actor_id='user')
            async with sessions() as session:
                assert (await session.get(LoopPendingDecision,gate.pending_decision_id)).status=='pending'
                assert (await session.get(LoopWaitRequest,wait['request_id'])).status=='open'
                assert (await session.get(AgentLoop,fixture['loop_id'])).status=='waiting_user'
                assert await session.scalar(select(func.count()).select_from(LoopWaitResponse).where(LoopWaitResponse.request_id==wait['request_id']))==0
                assert await session.scalar(select(func.count()).select_from(LoopEventOutbox).where(LoopEventOutbox.loop_id==fixture['loop_id'],
                    LoopEventOutbox.event_type=='LoopPendingDecisionSourceResolved'))==0
        finally:
            await _stop(fixture['service'],fixture['loop_id'])
            await engine.dispose()
    asyncio.run(run())
