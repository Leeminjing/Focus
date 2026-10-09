"""本文件对外提供工作区 Patrol 的缺省 Mission、空验收与共享进度查询合同回归。

输入为独立的用户要求及原始类型化输入，输出为缺省分区、稳定任务来源和禁止空验收的断言。
具体工作流为不调用网络或用户数据库，验证普通会话既有完整合同与新缺省合同共存，
再挂载两个生产 Loop router，通过进程内 HTTP 核对唯一进度入口与查询响应。
示例：python -m pytest backend/tests/test_workspace_patrol_contracts.py -q。
"""

from backend.app.desktop.agent_loop.mission_contract import LoopMissionContract
from backend.app.desktop.agent_loop.task_progress.consolidation import initial_progress
from backend.app.desktop.agent_loop.schemas import LoopCreateRequest
from pydantic import ValidationError
import pytest


def test_task_progress_has_one_shared_http_query(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.desktop.agent_loop.query_routes import loop_query_router
    from backend.app.desktop.agent_loop.routes import agent_loop_router
    from backend.app.desktop.agent_loop.task_progress.query import TaskProgressQuery

    query_session = object()
    reads = []
    committed = {"current": {"generation": 1}, "readiness": "ready"}

    @asynccontextmanager
    async def sessions():
        yield query_session

    async def read(self, session, loop_id):
        assert session is query_session
        reads.append(loop_id)
        return committed

    monkeypatch.setattr(TaskProgressQuery, "read", read)
    app = FastAPI()
    app.include_router(agent_loop_router)
    app.include_router(loop_query_router)
    app.state.desktop_service = SimpleNamespace(session_factory=sessions)
    routes = [
        route
        for route in app.routes
        if getattr(route, "path", None) == "/desktop/api/agent-loops/{loop_id}/task-progress"
        and "GET" in route.methods
    ]
    assert len(routes) == 1
    with TestClient(app) as client:
        response = client.get("/desktop/api/agent-loops/current-loop/task-progress")
    assert response.status_code == 200
    assert response.json() == committed
    assert reads == ["current-loop"]


def test_mission_sections_can_be_independently_absent():
    mission = LoopMissionContract()
    assert mission.outcome is None
    assert mission.completion_checks == ()
    assert mission.boundaries.statements() == ()
    boundary = LoopMissionContract(boundaries={"required_invariants": ["只保存在本地"]})
    assert boundary.outcome is None and boundary.completion_checks == ()


def test_absent_mission_does_not_create_a_fabricated_task():
    progress = initial_progress({"outcome": None, "completion_checks": []}, 1)
    assert progress.items == ()


def test_context_loop_keeps_its_full_mission_start_contract():
    with pytest.raises(ValidationError, match="旧 Context Loop 启动仍需"):
        LoopCreateRequest(
            loop_id="old",
            workspace_id="workspace",
            initial_context_id="context",
            initial_run_id="run",
            holder_id="patrol",
            context_scope=["context"],
            capabilities=["continue_context"],
            permission_scope=["read"],
            mission=LoopMissionContract(),
        )
