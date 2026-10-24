"""验证输入反馈的工作区隔离、精确决策来源和停止清理边界。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

import pytest

from backend.app.desktop.patrol.agent_loop.inputs.input_feedback import PatrolInputFeedbackReader
from backend.app.desktop.patrol.agent_loop.kernel import LoopKernel
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopAction, LoopDecision, LoopObservation, LoopRound
from backend.app.desktop.patrol.agent_loop.inputs.patrol_inputs import PatrolInputRequest
from backend.app.desktop.persistence.models import DesktopRun, DesktopWorkspace
from backend.tests.test_workspace_patrol import _setup
from backend.tests.test_workspace_patrol_decisions import _decision, _prepare


@pytest.mark.usefixtures("isolated_postgres_database")
@pytest.mark.parametrize("disposition", ["decision", "discussion", "reference", "deferred"])
def test_history_returns_exact_applied_disposition_and_evidence(tmp_path, disposition):
    async def run():
        engine, sessions, workspace, service = await _setup(tmp_path)
        try:
            first = await service.submit(workspace, PatrolInputRequest(
                submission_id=uuid.uuid4().hex, content="先别改 UI，把导入失败解决。"))
            assert (await service.history(workspace))["items"][0]["processing_result"] is None
            await _prepare(sessions, first["loop_id"])
            explanation = "先定位导入失败；下一步验证错误原因。"
            changes = [{"intent_id": first["intent_id"], "section": "boundary", "quote": "先别改 UI",
                        "boundary_group": "prohibited_actions", "operation": "supplement"}] if disposition == "decision" else []
            intent = await _decision(sessions, first["loop_id"], [{
                "action": "apply_user_inputs", "inputs": [{
                    "intent_id": first["intent_id"], "disposition": disposition,
                    "explanation": explanation,
                }],
                "changes": changes,
            }])
            # 老夹具未填写新版冻结版本合同；此回归显式绑定当前 Observation。
            async with sessions() as session:
                current = await session.get(LoopRound, intent.round_id)
                observation = await session.get(LoopObservation, current.observation_id)
            intent = type(intent).model_validate({**intent.model_dump(),
                "observation_id": observation.observation_id,
                "observation_generation": observation.generation,
                "observed_envelope_hash": observation.envelope_hash,
                "evidence": ({"fact_id": "exact-source", "revision": 3},),
                "context_permissions": [{"context_id": item["context_id"], "access_mode": None}
                                        for item in observation.envelope["portfolio_frontier"]],
            })
            committed = await LoopKernel(sessions).commit(intent)
            assert committed.status == "committed", committed.reason
            page = await service.history(workspace)
            row = page["items"][0]
            result = row["processing_result"]
            assert row["status"] == "accepted"
            assert row["processing_state"] == ("pending" if disposition == "deferred" else "addressed")
            assert result["disposition"] == disposition and result["explanation"] == explanation
            assert result["decision_id"] == intent.decision_id
            assert result["observation_id"] == observation.observation_id
            assert result["observation_generation"] == observation.generation
            assert result["evidence"] == [{"fact_id": "exact-source", "revision": 3}]
            assert result["mission_revision"] == intent.goal_revision + bool(changes)
            assert [item["quote"] for item in result["changes"]] == [item["quote"] for item in changes]

            if disposition == "decision":
                # 读取生产表中的取消回执和 Run 结算；不能仅根据 interrupted 宣告停止。
                run_id = uuid.uuid4().hex
                async with sessions.begin() as session:
                    loop = await session.get(AgentLoop, intent.loop_id)
                    session.add(DesktopRun(run_id=run_id, task_id=loop.initial_context_id,
                        agent_id="lead_agent", kind="main", status="interrupted",
                        loop_id=loop.loop_id, round_id=intent.round_id))
                    session.add(LoopAction(action_id=uuid.uuid4().hex, decision_id=intent.decision_id,
                        loop_id=loop.loop_id, position=1, action_type="cancel_execution", status="applied",
                        payload={"target_kind": "run", "target_id": run_id, "round_id": intent.round_id,
                                 "reason": "先解决导入失败"}, result={"status": "cancel_requested"}))
                requested = (await service.history(workspace))["items"][0]["processing_result"]
                assert requested["related_cancellations"][0]["state"] == "cancel_requested"
                async with sessions.begin() as session:
                    run_row = await session.get(DesktopRun, run_id)
                    run_row.settled_at = datetime.now(UTC)
                result = (await service.history(workspace))["items"][0]["processing_result"]
                assert result["related_cancellations"][0]["state"] == "stopped"

            if disposition == "deferred":
                # 同一输入在下一轮被采用，最新反馈取代延期说明而不新增输入行。
                next_intent = await _decision(sessions, first["loop_id"], [{
                    "action": "apply_user_inputs", "inputs": [{"intent_id": first["intent_id"],
                    "disposition": "decision", "explanation": "已采用先前延期的输入。"}],
                }])
                async with sessions() as session:
                    current = await session.get(LoopRound, next_intent.round_id)
                    next_observation = await session.get(LoopObservation, current.observation_id)
                next_intent = type(next_intent).model_validate({**next_intent.model_dump(),
                    "observation_id": next_observation.observation_id,
                    "observation_generation": next_observation.generation,
                    "observed_envelope_hash": next_observation.envelope_hash,
                    "context_permissions": [{"context_id": item["context_id"], "access_mode": None}
                                            for item in next_observation.envelope["portfolio_frontier"]],
                })
                committed = await LoopKernel(sessions).commit(next_intent)
                assert committed.status == "committed", committed.reason
                page = await service.history(workspace)
                assert len(page["items"]) == 1
                result = page["items"][0]["processing_result"]
                assert result["disposition"] == "decision"
                assert result["decision_id"] == next_intent.decision_id
                intent = next_intent

            # 后续未授权的候选不得覆盖已经生效的说明。
            async with sessions.begin() as session:
                decision = await session.get(LoopDecision, intent.decision_id)
                candidate = LoopDecision(
                    decision_id=uuid.uuid4().hex, loop_id=decision.loop_id,
                    round_id=decision.round_id, holder_id=decision.holder_id,
                    intent={}, rationale="未生效", status="rejected", idempotency_key=uuid.uuid4().hex,
                )
                session.add(candidate)
                await session.flush()
                session.add(LoopAction(
                    action_id=uuid.uuid4().hex, decision_id=candidate.decision_id,
                    loop_id=decision.loop_id, position=0, action_type="apply_user_inputs",
                    payload={"inputs": [{"intent_id": first["intent_id"], "disposition": "decision", "explanation": "不应出现"}]},
                    result={"input_ids": [first["intent_id"]]}, status="applied",
                ))
            assert (await service.history(workspace))["items"][0]["processing_result"] == result
            # 指定身份查询也遵守原工作区边界，且不改变分页合同。
            assert (await service.history(workspace, intent_ids=[first["intent_id"]]))["items"][0]["processing_result"] == result
            other = uuid.uuid4().hex
            async with sessions.begin() as session:
                session.add(DesktopWorkspace(workspace_id=other, path=str(tmp_path / "other"), display_name="other"))
            assert (await service.history(other, intent_ids=[first["intent_id"]]))["items"] == []
            from fastapi import FastAPI
            from httpx import ASGITransport, AsyncClient
            from backend.app.desktop.patrol.agent_loop.routes import agent_loop_router

            app = FastAPI()
            app.include_router(agent_loop_router)
            app.state.workspace_patrol = service
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                path = f"/desktop/api/agent-loops/workspace/{workspace}/inputs"
                response = await client.get(path, params={"intent_ids": first["intent_id"]})
                assert response.status_code == 200
                assert response.json()["items"][0]["processing_result"] == result
                assert (await client.get(path, params=[("intent_ids", "x")] * 101)).status_code == 422
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("kind, target, run, expected", [
    ("run", SimpleNamespace(status="interrupted", settled_at=None), None, "cancel_requested"),
    ("run", SimpleNamespace(status="interrupted", settled_at=datetime.now(UTC)), None, "stopped"),
    ("run", SimpleNamespace(status="completed", settled_at=datetime.now(UTC)), None, "settled"),
    ("worker", SimpleNamespace(status="cancelled", completed_at=None), None, "cancel_requested"),
    ("worker", SimpleNamespace(status="cancelled", completed_at=datetime.now(UTC)), None, "stopped"),
    ("directive", SimpleNamespace(launched_run_id="run", lifecycle_state="cancelled"),
     SimpleNamespace(status="interrupted", settled_at=None), "cancel_requested"),
    ("directive", SimpleNamespace(launched_run_id=None, lifecycle_state="cancelled"), None, "stopped"),
    ("run", None, None, "unknown"),
])
def test_stop_feedback_waits_for_actual_cleanup(kind, target, run, expected):
    assert PatrolInputFeedbackReader.cancellation_state({"status": "cancel_requested"}, kind, target, run) == expected
    assert PatrolInputFeedbackReader.cancellation_state({"status": "already_terminal"}, kind, target, run) == "already_terminal"
