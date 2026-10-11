"""本文件对外提供 running Context 预览的真实 HTTP、SSE 与浏览器验收。

输入为隔离 PostgreSQL、真实工作目录及仅控制采样时序的模型；输出为并行 Run、角色切换、尾部预览和终态的证据。
具体工作流为生产 Bootstrap 发布 checkpoint、Kernel 授权四个 Context、ContextRunPool/Dispatcher 启动真实图，再由 Electron 经生产路由消费真实事件。
仅暂停自主 Patrol 决策并控制模型与只读工具的屏障，不伪造 Run、checkpoint、SSE 或成功状态；测试控制路由只在该用例内存在。
示例：python -m pytest backend/tests/test_running_context_preview_real.py -q -s。
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
from typing import Any
import uuid

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import StructuredTool
import pytest
from sqlalchemy import select

from http_asgi_bridge import ASGIHTTPBridge
from test_session_patrol_workbench_api import client as client, SESSION


class _SamplingControl:
    def __init__(self, key):
        self.key = key
        self.phase = "starting"
        self.gates = {name: asyncio.Event() for name in ("assistant", "tool", "result", "final", "finish")}


class _ControlledModel(BaseChatModel):
    control: Any
    controls: Any
    calls: int = 0

    @property
    def _llm_type(self):
        return "real-preview-controlled-sampling"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("The actual worker must use streaming")

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.control.key = str(len(self.controls))
            self.controls.append(self.control)
            await self.control.gates["assistant"].wait()
            self.control.phase = "assistant"
            for content in (f"线程 {self.control.key} 正在读取工作区。\n", "中间内容应离开窗口。\n", "最后一行：中文与 emoji 👩🏽‍💻 实时输出。"):
                yield ChatGenerationChunk(message=AIMessageChunk(content=content, id=f"assistant-{self.control.key}"))
                await asyncio.sleep(0.08)
            await self.control.gates["tool"].wait()
            self.control.phase = "tool_wait"
            yield ChatGenerationChunk(message=AIMessageChunk(content="", id=f"assistant-{self.control.key}", tool_call_chunks=[
                {"name": "read_file", "args": '{"path":"source.md"}', "id": f"call-{self.control.key}", "index": 0},
            ]))
        else:
            self.control.phase = "tool_result"
            await self.control.gates["final"].wait()
            self.control.phase = "assistant_final"
            for content in (f"线程 {self.control.key} 已核对真实文件。\n", "最后两行之一。\n", "最终一行：已保留实际工具结果。"):
                yield ChatGenerationChunk(message=AIMessageChunk(content=content, id=f"final-{self.control.key}"))
                await asyncio.sleep(0.08)
            await self.control.gates["finish"].wait()
            self.control.phase = "finished"


async def _authorize_contexts(service, loop_id, extra_context_ids):
    from backend.app.desktop.agent_loop.models import AgentLoop, LoopContextMembership, LoopDelegationGrant, LoopRound
    from backend.app.desktop.agent_loop.rounds import current_frontier_hash
    from backend.app.desktop.context_curation.models import CurationLane
    from backend.app.desktop.context_evolution.schemas import ContextRevisionOriginKind
    from backend.app.desktop.models import DesktopThread

    sessions = service.session_factory
    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, loop_id)
        context = await session.get(DesktopThread, loop.initial_context_id)
        context.title = "并行线程甲"
        contexts = [(context.task_id, context.current_revision_id)]
        for index, context_id in enumerate(extra_context_ids, 1):
            second = await session.get(DesktopThread, context_id)
            second.title = f"并行线程 {index}"
            revision = await service.contexts.evolution.stage_definition(session, context_id,
                ({"id": uuid.uuid4().hex, "role": "human", "content": f"并行工作线程 {index}"},), (), ContextRevisionOriginKind.ROOT, "preview-fixture")
            lane_id = uuid.uuid4().hex
            session.add(CurationLane(lane_id=lane_id, program_id=loop.program_id, managed_context_id=context_id,
                purpose=f"并行线程 {index}", normalized_purpose=f"并行线程 {index}", lane_policy={"workspace_mode": "read_only"}))
            await session.flush()
            session.add(LoopContextMembership(membership_id=uuid.uuid4().hex, loop_id=loop_id,
                context_id=context_id, lane_id=lane_id, role="side", status="active"))
            contexts.append((context_id, revision.ref.revision_id))
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id))
        grant.context_scope = [item[0] for item in contexts]
        grant.permission_scope = ["read"]
        loop.equipment = {**loop.equipment, "permissions": ["read"]}
        await session.flush()
        round_row = await session.get(LoopRound, loop.current_round_id)
        round_row.frontier_hash = await current_frontier_hash(session, loop_id)
    return await _commit_contexts(service, loop_id, contexts), contexts


async def _commit_contexts(service, loop_id, contexts):
    from backend.app.desktop.agent_loop import LoopKernel, PatrolDecisionIntent
    from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopRound

    sessions = service.session_factory
    async with sessions() as session:
        loop = await session.get(AgentLoop, loop_id)
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.loop_id == loop_id))
        round_row = await session.get(LoopRound, loop.current_round_id)
        intent = PatrolDecisionIntent(decision_id=uuid.uuid4().hex, idempotency_key=uuid.uuid4().hex,
            loop_id=loop_id, loop_revision=loop.revision, round_id=round_row.round_id, holder_id=loop.holder_id,
            grant_id=grant.grant_id, grant_revision=grant.revision, goal_revision=loop.goal_revision,
            observed_frontier_hash=round_row.frontier_hash, observed_workspace_revision=round_row.workspace_revision,
            rationale="读取真实文件并验证并行预览", actions=tuple({"action": "continue_context", "context_id": context_id,
                "context_revision_id": revision_id, "message": "读取 source.md 并报告末两行"} for context_id, revision_id in contexts))
    result = await LoopKernel(sessions).commit(intent)
    assert result.status == "committed", result
    return intent.round_id


async def _dispatch(app, loop_id, round_id, expected):
    from backend.app.desktop.models import DesktopRun

    assert await app.state.agent_loop_context_runs.drain(loop_id) == 1
    for _ in range(300):
        async with app.state.desktop_service.session_factory() as session:
            rows = list((await session.scalars(select(DesktopRun).where(
                DesktopRun.loop_id == loop_id, DesktopRun.round_id == round_id).order_by(DesktopRun.created_at))).all())
        if len(rows) == expected and all(row.status == "running" for row in rows):
            return [row.run_id for row in rows]
        if any(row.status == "error" for row in rows):
            raise AssertionError([(row.run_id, row.status, row.error) for row in rows])
        await asyncio.sleep(0.05)
    raise AssertionError("Actual ContextRunPool did not start the authorized wave")


def test_parallel_contexts_real_run_sse_and_browser(client, tmp_path, monkeypatch):
    import backend.app.desktop.service as service_module
    from backend.app.desktop.agent_loop.workspace_patrol import WorkspacePatrolBootstrap
    from backend.app.gateway.app import app

    repo = Path(__file__).resolve().parents[2]
    electron = repo / "desktop/node_modules/electron/dist/electron.exe"
    if not electron.is_file():
        pytest.skip("Electron executable unavailable")
    http, task, workspace, _requests = client
    controls = []

    async def make_graph(**kwargs):
        control = _SamplingControl("unstarted")

        async def read_file(path: str):
            await control.gates["result"].wait()
            return (workspace / path).read_text(encoding="utf-8")

        return create_agent(model=_ControlledModel(control=control, controls=controls), tools=[
            StructuredTool.from_function(coroutine=read_file, name="read_file", description="Read the isolated workspace file"),
        ])

    monkeypatch.setattr(service_module, "make_lead_agent", make_graph)
    http.portal.call(app.state.agent_loop_runtime.close)
    (workspace / "source.md").write_text("真实文件首行\n工具结果倒数第二行。\n工具结果最后一行：并行隔离。", encoding="utf-8")
    receipt = http.post(f"/desktop/api/agent-loops/workspace/{task['workspace_id']}/inputs", headers=SESSION,
        json={"submission_id": uuid.uuid4().hex, "content": "并行线程甲", "input_type": "information", "access_mode": "read-only"})
    assert receipt.status_code == 200, receipt.text
    loop_id = receipt.json()["loop_id"]
    service = app.state.desktop_service
    http.portal.call(WorkspacePatrolBootstrap(service.session_factory, service.contexts.evolution).prepare, loop_id)
    extra_ids = [task["task_id"]]
    for index in range(2):
        response = http.post(f"/desktop/api/workspaces/{task['workspace_id']}/threads", headers=SESSION,
            json={"thread_id": "preview-" + uuid.uuid4().hex, "title": f"并行线程 {index + 2}"})
        assert response.status_code == 200, response.text
        extra_ids.append(response.json()["task_id"])
    round_id, contexts = http.portal.call(_authorize_contexts, service, loop_id, extra_ids)
    run_ids = http.portal.call(_dispatch, app, loop_id, round_id, 4)
    assert len(run_ids) == 4

    async def control_status():
        return {"controls": [{"key": value.key, "phase": value.phase} for value in controls],
            "connections": bridge.active_paths(), "runs": [await service.get_run_payload(run_id) for run_id in run_ids]}

    async def advance(payload: dict):
        if payload["phase"] == "disconnect":
            return {"disconnected": bridge.disconnect(f"/desktop/api/runs/{payload['run_id']}/stream")}
        if payload["phase"] == "restart":
            from backend.app.desktop.models import DesktopThread

            await app.state.agent_loop_run_events.drain("loop-coordinator", app.state.agent_loop_coordinator.handle_run_settled, loop_id=loop_id)
            async with service.session_factory() as session:
                context = await session.get(DesktopThread, contexts[0][0])
                revised = (context.task_id, context.current_revision_id)
            next_round = await _commit_contexts(service, loop_id, [revised])
            replacement = await _dispatch(app, loop_id, next_round, 1)
            assert len(replacement) == 1
            run_ids.extend(replacement)
            return {"run_id": replacement[0], "context_id": revised[0]}
        for value in controls:
            if payload.get("key") is None or value.key == str(payload["key"]):
                value.gates[payload["phase"]].set()
        return await control_status()

    app.add_api_route("/desktop/api/__test__/running-preview", control_status, methods=["GET"])
    app.add_api_route("/desktop/api/__test__/running-preview", advance, methods=["POST"])
    routes = app.router.routes[-2:]
    del app.router.routes[-2:]
    app.router.routes[:0] = routes
    evidence = repo / ".tmp-focus-running-preview-evidence"
    evidence.mkdir(exist_ok=True)
    result_file = tmp_path / "running-ui.json"
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env.update(FOCUS_TASK_REAL_EVIDENCE=str(evidence), FOCUS_RUNNING_REAL_RESULT=str(result_file),
        FOCUS_RUNNING_REAL_CONFIG=json.dumps({"workspace_id": task["workspace_id"], "loop_id": loop_id, "contexts": contexts, "run_ids": run_ids}))
    traffic = []
    try:
        with ASGIHTTPBridge(http, headers=SESSION, traffic=traffic) as bridge:
            env["FOCUS_TASK_REAL_URL"] = bridge.url
            print(f"REAL_PREVIEW_URL={bridge.url}", flush=True)
            result = subprocess.run([str(electron), str(repo / "desktop/running-context-real.e2e.cjs")], cwd=repo, env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=110)
            assert result.returncode == 0, result.stdout + result.stderr + str(traffic[-30:])
            ui = json.loads(result_file.read_text(encoding="utf-8"))
            for run_id in run_ids:
                run = http.get(f"/desktop/api/runs/{run_id}", headers=SESSION).json()
                assert run["status"] == "success", run
                assert run["context_revision_id"] and run["execution_thread_id"]
                assert ("GET", f"/desktop/api/runs/{run_id}/stream", 200) in traffic
            assert any(request["last_event_id"] for request in bridge.requests if request["path"].endswith("/stream"))
            (evidence / "real-running-result.json").write_text(json.dumps({"ui": ui, "traffic": traffic, "requests": bridge.requests}, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        async def release():
            for value in controls:
                for gate in value.gates.values():
                    gate.set()
        http.portal.call(release)
        for route in routes:
            app.router.routes.remove(route)
