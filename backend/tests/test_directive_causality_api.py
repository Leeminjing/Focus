"""本文件验证执行详情的 Directive 因果查询 HTTP 接入。

输入为真实 PostgreSQL 中的决策、Directive 状态、Run、工作树 anchor 和相关/无关事件；输出为完整有序因果链。
具体工作流为播种已持久的决策与 Directive，再沿生产生命周期记录启动及结算，HTTP 读取后核对只读性与历史兼容。
示例：pytest backend/tests/test_directive_causality_api.py。
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.agent_loop import AgentLoopService, LoopCreateRequest
from backend.app.desktop.agent_loop.directive_causality import DirectiveCausalityQuery, DirectiveCausalityRecorder
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.models import LoopAction, LoopDecision, LoopDirective
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.routes import desktop_router
from backend.app.desktop.service import DesktopService
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceSlot
from backend.tests.test_loop_live_acceptance import _seed_contexts

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


def test_execution_details_read_complete_persisted_causality(tmp_path):
    async def run():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        workspace_id, context_id, revision_id, loop_id, initial_run_id, run_id, slot_id = (uuid.uuid4().hex for _ in range(7))
        try:
            await _seed_contexts(sessions, tmp_path, workspace_id, (context_id,), (revision_id,), initial_run_id, "causality")
            snapshot = await AgentLoopService(sessions).start(LoopCreateRequest(
                loop_id=loop_id, workspace_id=workspace_id, initial_context_id=context_id,
                initial_run_id=initial_run_id, holder_id="patrol-1", goal="检查结果",
                task_contract="仅查询因果链", acceptance_criteria=({"criterion_id": "done", "text": "通过"},),
                capabilities=("continue_context", "request_completion"), context_scope=(context_id,), permission_scope=("read",),
                equipment={"permissions": ["read"]},
            ))
            round_id = snapshot["current_round_id"]
            directive_id, decision_id, action_id = (uuid.uuid4().hex for _ in range(3))
            async with sessions.begin() as session:
                session.add(LoopDecision(decision_id=decision_id, loop_id=loop_id, round_id=round_id,
                    holder_id="patrol-1", intent={}, rationale="发现失败，需要检查结果", status="committed", idempotency_key=decision_id))
                await session.flush()
                session.add(LoopAction(action_id=action_id, decision_id=decision_id, loop_id=loop_id,
                    position=0, action_type="continue_context", payload={}, status="committed"))
                await session.flush()
                directive = LoopDirective(directive_id=directive_id, loop_id=loop_id, round_id=round_id,
                    decision_id=decision_id, action_id=action_id, target_context_id=context_id,
                    target_context_revision_id=revision_id, message_id=directive_id, content="检查 result.txt",
                    content_hash="a" * 64, actor_id="patrol-1", grant_id=snapshot["grant"]["grant_id"],
                    grant_revision=1, goal_revision=1, correlation_id=directive_id, idempotency_key=directive_id)
                session.add(directive)
                lifecycle = DirectiveLifecycleRepository()
                await lifecycle.register(session, directive)
                await lifecycle.transition(session, directive_id, "authorized")
                session.add(WorkspaceSlot(slot_id=slot_id, workspace_id=workspace_id, kind="isolated",
                    root_path=str(tmp_path / "isolated"), current_fingerprint="b" * 64))
                execution = DesktopRun(run_id=run_id, task_id=context_id, agent_id=f"main:{context_id}", kind="main",
                    status="success", origin="delegated_patrol", directive_id=directive_id, loop_id=loop_id,
                    round_id=round_id, workspace_result={"files_changed": 1}, settled_at=datetime.now(UTC))
                session.add(execution)
                await session.flush()
                session.add(RunExecutionAnchor(run_id=run_id, slot_id=slot_id, directive_id=directive_id,
                    observed_workspace_revision=1, resulting_workspace_revision=2,
                    observed_fingerprint="a" * 64, resulting_fingerprint="b" * 64,
                    effect_evidence=[{"path": "result.txt", "operation": "modified"}]))
                for state in ("delivering", "delivered", "run_started"):
                    await lifecycle.transition(session, directive_id, state, run_id=run_id)
                recorder = DirectiveCausalityRecorder()
                await recorder.run_started(session, directive, run_id)
                await recorder.run_settled(session, directive, execution, None)
                await lifecycle.transition(session, directive_id, "settled", run_id=run_id, reason="运行完成")
                await LoopEventJournal().append(session, loop_id, CanonicalEventDraft(
                    kind="context.run.started", entity_type="context_run", entity_id="unrelated",
                    entity_revision=1, correlation_id="other-correlation", payload={"run_id": "unrelated"},
                    idempotency_key="unrelated",
                ))
            desktop = DesktopService.__new__(DesktopService)
            desktop.session_factory = sessions
            app = FastAPI()
            app.state.desktop_service = desktop
            app.include_router(desktop_router)
            async with sessions() as session:
                before = await DirectiveCausalityQuery().read(session, directive_id)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get(f"/desktop/api/runs/{run_id}/causality")
                assert response.status_code == 200
                chain = response.json()["causality"]
                assert chain["directive"]["content"] == "检查 result.txt"
                assert chain["decision"]["rationale"] == "发现失败，需要检查结果"
                assert [item["state"] for item in chain["transitions"]] == ["proposed", "authorized", "delivering", "delivered", "run_started", "settled"]
                assert chain["runs"][0]["run_id"] == run_id
                assert chain["workspace"][0]["root_path"] == str(tmp_path / "isolated")
                assert chain["workspace"][0]["effect_evidence"][0]["path"] == "result.txt"
                assert chain["workspace"][0]["resulting_workspace_revision"] == 2
                assert all(item["occurred_at"] for item in chain["transitions"])
                assert [item["sequence"] for item in chain["events"]] == sorted(item["sequence"] for item in chain["events"])
                assert all(item["entity_id"] != "unrelated" for item in chain["events"])
                assert {item["kind"] for item in chain["events"]} >= {"context.run.started", "context.run.settled", "context.workspace.changed"}
                assert (await client.get(f"/desktop/api/runs/{initial_run_id}/causality")).json() == {"run_id": initial_run_id, "causality": None}
                assert (await client.get("/desktop/api/runs/missing/causality")).status_code == 404
            async with sessions() as session:
                assert await DirectiveCausalityQuery().read(session, directive_id) == before
        finally:
            await engine.dispose()
    asyncio.run(run())
