"""本文件对外提供当前Kernel权限事实与冻结组合补充的真实PostgreSQL一致性验收。
输入为真实Loop/Grant/Observation及同Observation合法assessment supplement；输出为权限等待在模型与Kernel当前事实中一致的断言。
具体工作流为使用独立测试库，在冻结前设定授权，复用原补充仓库与FactsReader，不创建执行或修改私有原生库。
示例：pytest backend/tests/test_clarification_permission_facts.py -q，包含损坏hash拒绝及旧基础assessment兼容。
"""
import asyncio,os,uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from backend.tests.test_patrol_session import _create_loop
from backend.tests.test_patrol_failure_diagnostics import _observation
from backend.app.desktop.agent_loop.models import AgentLoop,LoopRound,LoopObservation,LoopDelegationGrant,LoopGoalRevision
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from backend.app.desktop.agent_loop.decision_context import DecisionSupplementRepository,PatrolDecisionContext
from backend.app.desktop.agent_loop.observation import observation_hash
from backend.app.desktop.agent_loop.clarification_admission import ClarificationFacts,ClarificationFactsReader,ClarificationAdmissionPolicy
from backend.app.desktop.agent_loop.schemas import WaitForUserAction
from backend.app.desktop.agent_loop.task_progress.models import LoopDecisionSupplement


@pytest.mark.parametrize('need,capabilities,permissions,workspace,storage',[
    ('create_lane',('continue_context','wait_for_user'),('read','write'),'read_only','supplement'),
    ('write',('continue_context','wait_for_user','create_lane'),('read',),'isolated_write','supplement'),
    ('create_lane',('continue_context','wait_for_user'),('read','write'),'read_only','corrupt'),
    ('write',('continue_context','wait_for_user','create_lane'),('read',),'isolated_write','legacy'),
])
def test_kernel_permission_fact_reads_same_verified_expansion_supplement(need,capabilities,permissions,workspace,storage,tmp_path,isolated_postgres_database):
    async def run():
        engine=create_async_engine(os.environ['FOCUS_DATABASE_URL'])
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        service,snapshot=await _create_loop(sessions,tmp_path)
        try:
            async with sessions.begin() as session:
                loop=await session.get(AgentLoop,snapshot['loop_id'])
                round_row=await session.get(LoopRound,snapshot['current_round_id'])
                grant=await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id==loop.loop_id,LoopDelegationGrant.revision==loop.authority_revision))
                grant.capabilities=list(capabilities)
                grant.permission_scope=list(permissions)
                mission=await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id==loop.loop_id,LoopMissionRevision.revision==loop.goal_revision))
                legacy=await session.scalar(select(LoopGoalRevision).where(LoopGoalRevision.loop_id==loop.loop_id,LoopGoalRevision.revision==loop.goal_revision))
                effective=EffectiveMissionProjector.from_rows(structured=mission,legacy=legacy)
                assessment={'opportunities':[{'work_spec':{'workspace_requirement':workspace}}]}
                base=_observation(loop.loop_id,round_row.round_id,loop.revision).model_copy(deep=True,update={
                    'mission':effective.model_payload(),'grant':{'grant_id':grant.grant_id,'holder_id':grant.holder_id,
                        'capabilities':list(capabilities),'permission_scope':list(permissions),'context_scope':list(grant.context_scope)},
                    'portfolio_frontier':({'context_id':loop.initial_context_id},)})
                if storage=='legacy':
                    base=base.model_copy(deep=True,update={'expansion_assessment':assessment})
                observation_id=uuid.uuid4().hex
                session.add(LoopObservation(observation_id=observation_id,loop_id=loop.loop_id,round_id=round_row.round_id,
                    envelope=base.model_dump(mode='json'),envelope_hash=observation_hash(base)))
                round_row.observation_id=observation_id
                await session.flush()
                if storage!='legacy':
                    await DecisionSupplementRepository().put(session,observation_id,'expansion_assessment',assessment)
                if storage=='corrupt':
                    stored=await session.scalar(select(LoopDecisionSupplement).where(LoopDecisionSupplement.observation_id==observation_id,LoopDecisionSupplement.kind=='expansion_assessment'))
                    stored.payload={'opportunities':[]}
            composed=PatrolDecisionContext(base,expansion_assessment=assessment).model_observation()
            frozen_facts=ClarificationFacts.from_observation(composed)
            assert frozen_facts.permission_needs==frozenset({need})
            async with sessions() as session:
                loop=await session.get(AgentLoop,snapshot['loop_id'])
                round_row=await session.get(LoopRound,snapshot['current_round_id'])
                grant=await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id==loop.loop_id,LoopDelegationGrant.revision==loop.authority_revision))
                if storage=='corrupt':
                    with pytest.raises(ValueError,match='补充 hash'):
                        await ClarificationFactsReader().read(session,loop,grant,round_row)
                    return
                current=await ClarificationFactsReader().read(session,loop,grant,round_row)
                assert current.permission_needs==frozen_facts.permission_needs
                ClarificationAdmissionPolicy().validate(WaitForUserAction(action='wait_for_user',reason='specific permission',
                    cause='permission',required_input=f'authorize {need}',evidence_identity={'kind':'capability','reference_id':need,'revision':loop.goal_revision}),current)
        finally:
            state=await service.get(snapshot['loop_id'])
            if state['status'] in {'running','paused','waiting_user'}:
                await service.control(snapshot['loop_id'],'stop')
            await engine.dispose()
    asyncio.run(run())
