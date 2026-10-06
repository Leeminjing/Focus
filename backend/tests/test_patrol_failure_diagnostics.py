"""本文件对外提供 Patrol 长提案合同拒绝及脱敏审计的生产入口验收。
输入为脚本化 Provider、实际 StructuredPatrolDecisionModel 与隔离 PostgreSQL attempt；输出为拒绝不变、动作诊断保真及零新执行断言。
具体工作流为长 rationale 后放非法等待动作，走原模型/合同/持久化入口，核对原文前缀与结构化诊断分别保存；
示例：pytest backend/tests/test_patrol_failure_diagnostics.py -q；受控 Provider 不替代原生验收。
"""
import asyncio
import hashlib
import json
import os

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop import round_orchestration as module
from backend.app.desktop.agent_loop.models import LoopDecision, LoopPatrolAttempt
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.agent_loop.patrol import PatrolContractViolation, PortfolioPatrol
from backend.app.desktop.agent_loop.patrol_audit import proposal_failure_diagnostic
from backend.app.desktop.agent_loop.round_orchestration import StructuredPatrolDecisionModel
from backend.app.desktop.agent_loop.schemas import ContinueContextAction, LoopObservationEnvelope, WaitForUserAction
from backend.tests.config_helpers import app_config_for
from backend.tests.test_patrol_session import _create_loop


def _observation(loop_id="loop", round_id="round", revision=1):
    return LoopObservationEnvelope(loop_id=loop_id, round_id=round_id, loop_revision=revision,
        goal_revision=1, authority_revision=1, observed_frontier_hash="a"*64,
        mission={"outcome":"review", "completion_checks":[{"check_id":"check-1"}]},
        grant={"holder_id":"holder", "grant_id":"grant", "capabilities":["continue_context"], "context_scope":["context"]},
        portfolio_frontier=({"context_id":"context"},), workspace={"revision":1}, budget={})


def _model(monkeypatch):
    payload={"rationale":"已有原文需要核对。"*280,
        "mission_references":[{"role":"outcome","reference_id":"outcome"}],
        "actions":[{"action":"wait_for_user","reason":"请求确认", "cause":"external_blocker",
            "required_input":"synthetic-secret-required-input", "evidence_identity":{"kind":"external", "reference_id":"synthetic-secret-reference", "revision":1}}]}
    class Provider:
        calls=0
        async def ainvoke(self,messages,**kwargs):
            self.calls+=1
            return AIMessage(content=json.dumps(payload,ensure_ascii=False))
    provider=Provider()
    monkeypatch.setattr(module,"create_chat_model",lambda **kwargs:provider)
    config=app_config_for("patrol-audit-test",None)
    config.models[0].curation_output_method="prompt_json"
    config.models[0].curation_max_output_tokens=4096
    return StructuredPatrolDecisionModel(config),provider


def test_long_parsed_proposal_keeps_rejected_actions_without_raw_secrets(monkeypatch):
    model,provider=_model(monkeypatch)
    with pytest.raises(PatrolContractViolation,match="cause 与当前") as failure:
        asyncio.run(model(_observation()))
    assert len(failure.value.raw_output)==2000
    assert "wait_for_user" not in failure.value.raw_output
    actions=getattr(failure.value,"proposal_actions",None)
    assert actions is not None
    assert actions[0].cause=="external_blocker"
    assert provider.calls==model.call_count==1


def test_failed_attempt_persists_safe_diagnostic_beyond_raw_prefix(tmp_path,monkeypatch,isolated_postgres_database):
    async def run():
        engine=create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        service,snapshot=await _create_loop(sessions,tmp_path)
        try:
            model,provider=_model(monkeypatch)
            observation=_observation(snapshot["loop_id"],snapshot["current_round_id"],snapshot["revision"])
            async with sessions() as session:
                baseline_runs=set(await session.scalars(select(DesktopRun.run_id).where(DesktopRun.loop_id==snapshot["loop_id"])))
            with pytest.raises(PatrolContractViolation):
                await PortfolioPatrol(sessions,model).decide(observation,snapshot["holder_id"])
            async with sessions() as session:
                attempt=await session.scalar(select(LoopPatrolAttempt).where(LoopPatrolAttempt.loop_id==snapshot["loop_id"]))
                assert attempt.status=="error" and attempt.completed_at
                diagnostic=attempt.raw_output.get("proposal_diagnostic")
                assert diagnostic is not None
                assert diagnostic["version"]=="patrol-proposal-diagnostic-v1"
                assert diagnostic["action_types"]==["wait_for_user"]
                wait=diagnostic["wait_requests"][0]
                assert wait["position"]==0 and wait["cause"]=="external_blocker"
                assert wait["evidence_kind"]=="external" and wait["evidence_revision"]==1
                assert wait["reference_identity"]=={"sha256":hashlib.sha256(b"synthetic-secret-reference").hexdigest(),"chars":26}
                assert "synthetic-secret" not in json.dumps(diagnostic)
                assert "已有原文" not in json.dumps(diagnostic)
                assert attempt.raw_output["decision_context"] and len(attempt.raw_output["raw_text"])==2000
                assert await session.scalar(select(func.count()).select_from(LoopDecision).where(LoopDecision.loop_id==snapshot["loop_id"]))==0
                assert set(await session.scalars(select(DesktopRun.run_id).where(DesktopRun.loop_id==snapshot["loop_id"])))==baseline_runs
            assert provider.calls==1
        finally:
            loop=await service.get(snapshot["loop_id"])
            if loop["status"] in {"running","paused","waiting_user"}:
                await service.control(snapshot["loop_id"],"stop")
            await engine.dispose()
    asyncio.run(run())


def test_diagnostic_preserves_wait_positions_and_hashes_only_untrusted_text():
    secret="synthetic-private-value"
    continuation=ContinueContextAction(action="continue_context",context_id="context",context_revision_id="revision",message=secret)
    waits=tuple(WaitForUserAction(action="wait_for_user",reason=secret,cause=cause,required_input=secret,
        evidence_identity={"kind":"external","reference_id":secret,"revision":2}) for cause in ("permission","external_blocker"))
    diagnostic=proposal_failure_diagnostic((continuation,*waits))
    assert diagnostic["action_types"]==("continue_context","wait_for_user","wait_for_user")
    assert [r["position"] for r in diagnostic["wait_requests"]]==[1,2]
    assert [r["cause"] for r in diagnostic["wait_requests"]]==["permission","external_blocker"]
    assert all(r["reference_identity"]==r["required_input_identity"] for r in diagnostic["wait_requests"])
    assert secret not in json.dumps(diagnostic)


def test_diagnostic_handles_legacy_wait_without_optional_fields():
    wait=WaitForUserAction(action="wait_for_user",reason="legacy")
    diagnostic=proposal_failure_diagnostic((wait,))
    assert diagnostic["wait_requests"][0]=={"position":0,"cause":None,"evidence_kind":None,"evidence_revision":None,
        "reference_identity":None,"required_input_identity":None}
