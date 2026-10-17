"""桌面装配范围的 API、Run 冻结、durable 重装配与权限边界验收。"""

import json
import os
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.app.gateway.app import app
from focus.security import AccessDecision, AccessOperation, decide_path_access, policy_from_context

SESSION = {"X-Focus-Session": "focus-dev-session"}


def _skill(root, description):
    path = root / ".agents" / "skills" / "shared" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: shared\ndescription: {description}\n---\n{description}", encoding="utf-8")


@pytest.mark.usefixtures("runtime_postgres_database")
def test_assembly_scope_binds_catalog_permissions_and_durable_run(tmp_path, monkeypatch):
    import backend.app.desktop.service as module

    home = tmp_path / "global"
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(home))
    _skill(home, "global shared")
    (home / "extensions_config.json").write_text(json.dumps({"mcpServers": {"playwright": {"enabled": False}}}), encoding="utf-8")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _skill(a, "only A")
    captured, pooled_workspaces = [], []

    async def pool(*, workspace=None):
        pooled_workspaces.append(workspace)
        return []

    async def make_agent(**kwargs):
        captured.append(kwargs)
        return object()

    async def idle_dispatch(self, **kwargs):
        return 0

    monkeypatch.setattr(module, "get_available_tools", pool)
    monkeypatch.setattr(module, "make_lead_agent", make_agent)
    monkeypatch.setattr(module.DurableRunDispatchWorker, "drain", idle_dispatch)

    with TestClient(app, client=("127.0.0.1", 50000)) as http:
        service = app.state.desktop_service

        workspace_a = http.post("/desktop/api/workspaces", headers=SESSION, json={"path": str(a)}).json()
        workspace_b = http.post("/desktop/api/workspaces", headers=SESSION, json={"path": str(b)}).json()
        global_task = http.get("/desktop/api/assembly/task", headers=SESSION).json()
        task_a = http.get("/desktop/api/assembly/task", headers=SESSION, params={"workspace_id": workspace_a["workspace_id"]}).json()
        task_b = http.get("/desktop/api/assembly/task", headers=SESSION, params={"workspace_id": workspace_b["workspace_id"]}).json()
        assert len({task["task_id"] for task in (global_task, task_a, task_b)}) == 3
        assert http.get("/desktop/api/assembly/task", headers=SESSION, params={"workspace_id": workspace_a["workspace_id"]}).json()["task_id"] == task_a["task_id"]
        assert http.get("/desktop/api/assembly/task?workspace_id=missing", headers=SESSION).status_code == 404
        assert task_a["assembly_target"] == {"scope": "workspace", "workspace_id": workspace_a["workspace_id"], "path": str(a)}
        assert global_task["assembly_target"]["scope"] == "global"
        for task, expected in ((global_task, "global shared"), (task_a, "only A"), (task_b, "global shared")):
            skills = http.get(f"/desktop/api/tasks/{task['task_id']}/skills", headers=SESSION).json()["skills"]
            assert next(skill["description"] for skill in skills if skill["name"] == "shared") == expected

            async def prepare():
                return await service.start_main_run(task["task_id"], "configure", None, ["read", "write"], ["shared"], access_mode="workspace-write")

            prepared = http.portal.call(prepare)
            assert prepared.body.context["workspace"] == task["workspace_path"]
            policy = policy_from_context(prepared.body.context)
            if task is task_a:
                assert home.resolve() not in policy.roots
                assert decide_path_access(policy, Path.home() / "unrelated-focus-workspace" / "note.txt", AccessOperation.WRITE) is AccessDecision.DENY
                assert decide_path_access(policy, a / ".focus" / "extensions_config.json", AccessOperation.WRITE) is AccessDecision.ASK
            awaitable_factory = prepared.agent_factory
            http.portal.call(awaitable_factory)
            kwargs = captured[-1]
            describe = next(tool for tool in kwargs["tools"] if tool.name == "describe_skill")
            assert expected in describe.invoke({"name": "shared"})
            assert expected in next(context.content for context in kwargs["frozen_contexts"] if context.name == "skill:shared")
            assert task["workspace_path"] in kwargs["system_prompt"]
            # Worker 重启后仍从持久 Run 装配相同目标与提示词。
            rebuilt = http.portal.call(service.assemble_run, prepared.payload["run_id"])
            assert rebuilt.body.context["workspace"] == prepared.body.context["workspace"]
            http.portal.call(rebuilt.agent_factory)
            assert captured[-1]["system_prompt"] == kwargs["system_prompt"]
        assert str(a) in pooled_workspaces and str(b) in pooled_workspaces and str(home) in pooled_workspaces
        _assert_windows_ui(http, workspace_a["workspace_id"], workspace_b["workspace_id"], b, tmp_path)


def _assert_windows_ui(http, workspace_a, workspace_b, path_b, tmp_path):
    """已安装 Electron 时补充真实 Windows UI；服务、路由、数据库均不替换。"""
    root = Path(__file__).resolve().parents[2]
    electron = Path(os.environ.get("FOCUS_ELECTRON_PATH", root / "desktop/node_modules/electron/dist/electron.exe"))
    if not electron.is_file():
        return
    from http_asgi_bridge import ASGIHTTPBridge

    evidence = Path(os.environ.get("FOCUS_ASSEMBLY_EVIDENCE", tmp_path / "screenshots"))
    evidence.mkdir(parents=True, exist_ok=True)
    user_data = tmp_path / "electron"
    user_data.mkdir()
    env = {**os.environ, "FOCUS_ASSEMBLY_USER_DATA": str(user_data), "FOCUS_ASSEMBLY_EVIDENCE": str(evidence),
           "FOCUS_ASSEMBLY_WORKSPACE_A": workspace_a, "FOCUS_ASSEMBLY_WORKSPACE_B": workspace_b,
           "FOCUS_ASSEMBLY_PICK": str(path_b)}
    env.pop("ELECTRON_RUN_AS_NODE", None)
    with ASGIHTTPBridge(http, headers=SESSION) as bridge:
        env["FOCUS_TASK_REAL_URL"] = bridge.url
        result = subprocess.run([str(electron), str(root / "desktop/assembly-scopes.e2e.cjs")], cwd=root,
                                env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "ASSEMBLY_SCOPES_UI_PASS" in result.stdout
