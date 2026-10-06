"""本文件对外提供等待准入模型读面的生产请求边界红绿验收。
输入为真实StructuredPatrolDecisionModel、冻结Observation及受控Provider；输出为精确合法等待集合和原输入不变断言。
具体工作流为在实际Provider边界捕获请求后退出，不调用真实模型、不创建Decision或Run；示例：pytest backend/tests/test_patrol_clarification_read.py -q。
纯政策组合另比较读面与最终验证，错误kind/reference/revision保持拒绝。
"""
import asyncio
import json
from dataclasses import replace
from itertools import product
import pytest
from backend.app.desktop.agent_loop import round_orchestration as module
from backend.app.desktop.agent_loop.round_orchestration import StructuredPatrolDecisionModel
from backend.app.desktop.agent_loop.clarification_admission import ClarificationAdmissionPolicy,ClarificationFacts,ClarificationRejected
from backend.app.desktop.agent_loop.schemas import WaitForUserAction
from backend.app.desktop.agent_loop.observation import observation_hash
from backend.tests.config_helpers import app_config_for
from backend.tests.test_patrol_failure_diagnostics import _observation


class RequestCaptured(RuntimeError):
    pass


@pytest.mark.parametrize('case,updates,expected',[
    ('safe',{},()),
    ('gate',{'pending_decisions':({'pending_decision_id':'gate-1','status':'pending','delegable':False},)},(('human_gate','gate','gate-1'),)),
    ('external',{'recovery_waiting_reason':'synthetic-external-proof'},(('external_blocker','external','recovery_waiting_reason'),)),
    ('permission',{'expansion_assessment':{'opportunities':[{'work_spec':{'workspace_requirement':'read_only'}}]}},(('permission','capability','create_lane'),)),
])
def test_provider_receives_exact_frozen_wait_admission(case,updates,expected,monkeypatch):
    captured={}
    class Provider:
        calls=0
        async def ainvoke(self,messages,**kwargs):
            self.calls+=1
            for message in messages:
                content=str(message.content)
                if '<loop_observation>' in content:
                    captured['payload']=json.loads(content.split('<loop_observation>',1)[1].split('</loop_observation>',1)[0])
            raise RequestCaptured
    provider=Provider()
    monkeypatch.setattr(module,'create_chat_model',lambda **kwargs:provider)
    config=app_config_for('clarification-read-test',None)
    config.models[0].curation_output_method='prompt_json'
    config.models[0].curation_max_output_tokens=4096
    model=StructuredPatrolDecisionModel(config)
    observation=_observation().model_copy(deep=True,update=updates)
    before=observation_hash(observation)
    with pytest.raises(RequestCaptured):
        asyncio.run(model(observation))
    view=captured['payload'].get('clarification_admission')
    assert view is not None
    assert view['version']=='clarification-admission-read-v1'
    assert view['mission_revision']==observation.goal_revision
    assert view['safe_continuation']==ClarificationFacts.from_observation(observation).safe_continuation
    actual=tuple(sorted((item['cause'],item['evidence_identity']['kind'],item['evidence_identity']['reference_id']) for item in view['admitted_requests']))
    assert actual==tuple(sorted(expected))
    assert all(item['evidence_identity']['revision']==observation.goal_revision for item in view['admitted_requests'])
    assert provider.calls==model.call_count==1
    assert observation_hash(observation)==before


def test_wait_read_projection_matches_existing_final_policy_for_all_identity_combinations():
    policy=ClarificationAdmissionPolicy()
    base=ClarificationFacts(mission_revision=3,outcome='complete',check_ids=frozenset({'check-1'}),
        capabilities=frozenset({'continue_context'}),pending_gate_ids=frozenset({'gate-1'}),
        budget_exhaustions=frozenset({'model_calls'}),external_blockers=frozenset({'recovery_waiting_reason'}),
        safe_continuation=False,missing_input_ids=frozenset({'input-1'}),permission_needs=frozenset({'create_lane'}))
    cases=(base,replace(base,safe_continuation=True),replace(base,active_run=True),replace(base,outcome=''),
        replace(base,check_ids=frozenset()),replace(base,outcome='',safe_continuation=True),
        replace(base,pending_gate_ids=frozenset(),budget_exhaustions=frozenset(),external_blockers=frozenset(),
            missing_input_ids=frozenset(),permission_needs=frozenset()))
    causes=('missing_goal','missing_input','human_gate','permission','budget','external_blocker')
    kinds=('mission','input','gate','capability','budget','external')
    references=('outcome','input-1','gate-1','create_lane','model_calls','recovery_waiting_reason','invented')
    for facts in cases:
        admitted=set()
        for cause,kind,reference in product(causes,kinds,references):
            action=WaitForUserAction(action='wait_for_user',reason='review',cause=cause,required_input='specific synthetic input',
                evidence_identity={'kind':kind,'reference_id':reference,'revision':facts.mission_revision})
            try:
                policy.validate(action,facts)
            except ClarificationRejected:
                continue
            admitted.add((cause,kind,reference))
            with pytest.raises(ClarificationRejected,match='revision'):
                policy.validate(action.model_copy(update={'evidence_identity':action.evidence_identity.model_copy(update={'revision':2})}),facts)
        view=policy.read_view(facts)
        projected={(item['cause'],item['evidence_identity']['kind'],item['evidence_identity']['reference_id']) for item in view['admitted_requests']}
        assert projected==admitted
        assert view['active_run']==facts.active_run
