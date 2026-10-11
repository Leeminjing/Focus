"""本文件对外提供传统任务、磁盘材料和实际 Run 的浏览器集成验收。

输入为复用的隔离 Desktop 服务、真实工作目录和受控采样图；输出为全图精确版本到任务、UI 与持久 Run/选材记录一致的断言及截图。
具体工作流为关闭本测试 Loop 自主调度后调用生产 Bootstrap 发布版本，共享 ASGI HTTP bridge 逐帧转发生产响应；替换模型采样而保留 Main/SSE、checkpoint 和材料读取，不调用外部 Provider。
示例：python -m pytest backend/tests/test_frontend_task_real.py -q。
"""
import json
import os
from pathlib import Path
import subprocess

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
import pytest
from test_session_patrol_workbench_api import client as client, SESSION
from http_asgi_bridge import ASGIHTTPBridge


def test_task_material_main_and_standalone_real_ui(client, tmp_path, monkeypatch):
    import backend.app.desktop.service as service_module

    async def make_graph(**kwargs):
        return create_agent(model=FakeListChatModel(responses=["真实任务响应"]), tools=[])

    monkeypatch.setattr(service_module, "make_lead_agent", make_graph)
    http, task, workspace, _requests = client
    from backend.app.gateway.app import app
    from backend.app.desktop.agent_loop.workspace_patrol import WorkspacePatrolBootstrap

    http.portal.call(app.state.agent_loop_runtime.close)
    receipt = http.post(f"/desktop/api/agent-loops/workspace/{task['workspace_id']}/inputs", headers=SESSION, json={
        "submission_id": "global-map-real", "content": "全图真实发布内容", "input_type": "information",
    })
    assert receipt.status_code == 200, receipt.text
    loop_id = receipt.json()["loop_id"]
    service = app.state.desktop_service
    http.portal.call(WorkspacePatrolBootstrap(service.session_factory, service.contexts.evolution).prepare, loop_id)
    assert http.post(f"/desktop/api/agent-loops/{loop_id}/control", headers=SESSION, json={"command": "pause"}).status_code == 200
    lineage = http.get(f"/desktop/api/agent-loops/{loop_id}/lineage", headers=SESSION).json()
    context_id, revision_id = next(iter(lineage["roots"].items()))
    source = workspace / "source.md"
    source.write_text("真实磁盘材料正文", encoding="utf-8")
    response = http.post(f"/desktop/api/tasks/{task['task_id']}/materials", headers=SESSION, json={"path": str(source)})
    assert response.status_code == 200, response.text
    material = response.json()
    groups_url = f"/desktop/api/tasks/{task['task_id']}/material-groups"
    first = http.post(groups_url, headers=SESSION, json={"name": "材料验收"}).json()
    second = http.post(groups_url, headers=SESSION, json={"name": "空分组"}).json()
    assert http.put(groups_url + "/order", headers=SESSION, json={"group_ids": [second["group_id"], first["group_id"]]}).status_code == 200
    membership_url = f"/desktop/api/tasks/{task['task_id']}/materials/{material['material_id']}/group"
    assert http.put(membership_url, headers=SESSION, json={"group_id": first["group_id"]}).status_code == 200
    assert [row["group_id"] for row in http.get(groups_url, headers=SESSION).json()] == [second["group_id"], first["group_id"]]
    assert http.delete(groups_url + f"/{first['group_id']}", headers=SESSION).status_code == 409
    assert http.delete(groups_url + f"/{second['group_id']}", headers=SESSION).status_code == 200
    traffic = []

    repo = Path(__file__).resolve().parents[2]
    electron = repo / "desktop/node_modules/electron/dist/electron.exe"
    if not electron.is_file():
        pytest.skip("Electron executable unavailable")
    evidence = repo / ".tmp-focus-frontend-evidence"
    evidence.mkdir(exist_ok=True)
    result_file = tmp_path / "task-ui.json"
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env.update(FOCUS_TASK_REAL_ID=task["task_id"], FOCUS_TASK_REAL_MATERIAL=material["material_id"], FOCUS_TASK_REAL_RESULT=str(result_file), FOCUS_TASK_REAL_EVIDENCE=str(evidence))
    env["FOCUS_MAP_REAL"] = json.dumps({"workspace_id": task["workspace_id"], "context_id": context_id, "revision_id": revision_id})
    with ASGIHTTPBridge(http, headers=SESSION, traffic=traffic) as bridge:
        env["FOCUS_TASK_REAL_URL"] = bridge.url
        result = subprocess.run([str(electron), str(repo / "desktop/frontend-task-real.e2e.cjs")], cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=80)
        assert result.returncode == 0, result.stdout + result.stderr + str(traffic[-25:])
        ui = json.loads(result_file.read_text(encoding="utf-8"))
        assert ui["map"] == {"context_id": context_id, "revision_id": revision_id, "opened_task_id": context_id}
        for route in (f"/desktop/api/agent-loops/{loop_id}/lineage", f"/desktop/api/agent-loops/{loop_id}/contexts/{context_id}/conversation", f"/desktop/api/tasks/{context_id}"):
            assert ("GET", route, 200) in traffic
        run = http.get(f"/desktop/api/runs/{ui['run_id']}", headers=SESSION).json()
        assert run["status"] == "success" and run["task_id"] == task["task_id"]
        history = http.get(f"/desktop/api/tasks/{task['task_id']}/material-history", headers=SESSION).json()
        assert any(row["run_id"] == ui["run_id"] and row["material_id"] == material["material_id"] for row in history)
        (evidence / "real-task-result.json").write_text(json.dumps(ui, ensure_ascii=False, indent=2), encoding="utf-8")
