"""本文件对外提供前端真实 Patrol 链路与截图的隔离验收。

输入为独立 PostgreSQL、临时目录、生产 Bootstrap 和 Electron；输出为四类真实 HTTP 输入、SSE/冻结检查、暂停保持及零虚假 Run 断言。
具体工作流为启动仅本用例的服务，UI 首次受理后保存实际 checkpoint，生产冻结后由 UI 检查；Provider 不参与本测试。
示例：python -m pytest backend/tests/test_frontend_migration_real.py -q。
"""

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import httpx
from langchain_core.messages import message_to_dict
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
import pytest
from sqlalchemy import func, select
from test_workspace_patrol import _setup

from focus.agents.lead_agent_state import LeadAgentState
from backend.app.desktop.agent_loop.models import AgentLoop, LoopUserIntent, LoopObservation
from backend.app.desktop.agent_loop.observation_capture import LoopObservationService
from backend.app.desktop.agent_loop.fact_models import LoopFact
from backend.app.desktop.domain_evidence.identity import canonical_hash
from backend.app.desktop.agent_loop.workspace_patrol import WorkspacePatrolBootstrap
from backend.app.desktop.context_evolution import ContextEvolutionService, ContextRevisionRepository
from backend.app.desktop.models import DesktopRun


@pytest.mark.usefixtures("runtime_postgres_database")
def test_real_patrol_frontend_intake_frozen_inspection_and_pause(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    electron = repo / "desktop/node_modules/electron/dist/electron.exe"
    if sys.platform != "win32" or not electron.is_file():
        pytest.skip("本原生交互验收需要 Windows Electron")

    async def run():
        engine, sessions, workspace_id, service = await _setup(tmp_path)
        saver = InMemorySaver()
        result = tmp_path / "result.json"
        checkpoint_file = tmp_path / "checkpoints.json"
        checkpoint_file.write_text("{}", encoding="utf-8")
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ)
        env.pop("ELECTRON_RUN_AS_NODE", None)
        env.update({
            "FOCUS_FRONTEND_SERVER": f"http://127.0.0.1:{port}",
            "FOCUS_FRONTEND_WORKSPACE": json.dumps({"workspace_id": workspace_id, "path": str(tmp_path), "display_name": "真实迁移验收"}, ensure_ascii=False),
            "FOCUS_FRONTEND_RESULT": str(result), "FOCUS_FRONTEND_CHECKPOINT": str(checkpoint_file),
            "FOCUS_FRONTEND_EVIDENCE": str(evidence),
        })
        server = desktop = None
        server_log = (tmp_path / "server.log").open("w", encoding="utf-8")
        desktop_log = (tmp_path / "desktop.log").open("w", encoding="utf-8")
        async def wait_result(key):
            for _ in range(350):
                try:
                    value = json.loads(result.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    value = {}
                if "error" in value:
                    raise AssertionError(value["error"])
                if key in value:
                    return value
                if desktop.poll() is not None:
                    raise AssertionError((tmp_path / "desktop.log").read_text(encoding="utf-8"))
                await asyncio.sleep(0.1)
            raise AssertionError(f"等待 Electron {key} 超时")
        try:
            server = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.tests.frontend_migration_server:app", "--host", "127.0.0.1", "--port", str(port)], cwd=repo, env=env, stdout=server_log, stderr=subprocess.STDOUT)
            async with httpx.AsyncClient(base_url=env["FOCUS_FRONTEND_SERVER"]) as client:
                for _ in range(150):
                    try:
                        response = await client.get(f"/desktop/api/agent-loops/workspace/{workspace_id}")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if server.poll() is not None:
                        raise AssertionError((tmp_path / "server.log").read_text(encoding="utf-8"))
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("隔离服务启动超时")
                desktop = subprocess.Popen([str(electron), str(repo / "desktop/frontend-migration-real.e2e.cjs")], cwd=repo, env=env, stdout=desktop_log, stderr=subprocess.STDOUT)
                ready = await wait_result("ready")
                loop_id = ready["loop_id"]
                async with sessions() as session:
                    assert await session.scalar(select(func.count()).select_from(LoopUserIntent).where(LoopUserIntent.loop_id == loop_id)) == 4
                    assert await session.scalar(select(func.count()).select_from(DesktopRun).where(DesktopRun.loop_id == loop_id)) == 0
                async def graph_factory():
                    builder = StateGraph(LeadAgentState)
                    builder.add_node("noop", lambda state: {})
                    builder.add_edge(START, "noop")
                    builder.add_edge("noop", END)
                    return builder.compile()
                await WorkspacePatrolBootstrap(sessions, ContextEvolutionService(sessions, saver, graph_factory)).prepare(loop_id)
                async with sessions() as session:
                    loop = await session.get(AgentLoop, loop_id)
                    revision = await ContextRevisionRepository().current(session, loop.initial_context_id)
                    checkpoint = await saver.aget_tuple(revision.ref.checkpoint_config())
                    checkpoint_file.write_text(json.dumps({revision.ref.checkpoint_id: [message_to_dict(message) for message in checkpoint.checkpoint["channel_values"]["messages"]]}, ensure_ascii=False), encoding="utf-8")
                    round_id = loop.current_round_id
                await LoopObservationService(sessions, saver).capture(loop_id, round_id)
                async with sessions.begin() as session:
                    session.add(LoopFact(
                        fact_id="frontend-unknown-fact", loop_id=loop_id,
                        identity_key=canonical_hash({"frontend": loop_id}), fact_type="test",
                        normalized_subject="frontend-unknown-count", state="observed",
                        presentation={"title": "真实持久事实，数量未知", "outcome_status": "unknown", "metrics": {"count_status": "unknown"}},
                        occurred_at=loop.created_at,
                    ))
                complete = await wait_result("complete")
                async with sessions() as session:
                    loop = await session.get(AgentLoop, loop_id)
                    assert loop.status == "paused"
                    observation = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == round_id))
                    assert observation.observation_id in complete["frozen"]
                    assert await session.scalar(select(func.count()).select_from(LoopUserIntent).where(LoopUserIntent.loop_id == loop_id)) == 5
                    assert await session.scalar(select(func.count()).select_from(DesktopRun).where(DesktopRun.loop_id == loop_id)) == 0
                assert len(list(evidence.glob("*.png"))) == 7
                output = repo / ".tmp-focus-frontend-evidence"
                output.mkdir(exist_ok=True)
                for source in evidence.glob("*.png"):
                    (output / source.name).write_bytes(source.read_bytes())
                (output / "real-patrol-result.json").write_text(json.dumps(complete, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            for child in (desktop, server):
                if child is not None and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill(); child.wait(timeout=10)
            server_log.close(); desktop_log.close()
            await engine.dispose()
    asyncio.run(run())
