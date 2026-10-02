"""本文件对外提供会话工作台实际 API、PostgreSQL、dispatch、create_agent 和请求投影集成验收。

输入为隔离数据库及受控模型采样，输出为草稿并发、编辑竞态、结构 buffer、来源失效、原定义及请求／ledger 断言。
工作流保留真实运行图、请求组装与持久 checkpoint，只替换模型响应；示例：pytest 本文件 -q。
"""
import asyncio
import uuid
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk
from sqlalchemy import select, text
from backend.app.gateway.app import app
from backend.app.desktop.models import PatrolDeploymentDefinition, PatrolDraft, DesktopRun
from focus.models.responses import FocusResponsesChatModel

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")
SESSION = {"X-Focus-Session": "focus-dev-session"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    requests = []
    async def stream(model, messages, stop=None, run_manager=None, **kwargs):
        requests.append(model.request_payload(messages, **kwargs))
        from langchain_core.outputs import ChatGenerationChunk
        yield ChatGenerationChunk(message=AIMessageChunk(content="完成"))
    monkeypatch.setattr(FocusResponsesChatModel, "_astream", stream)
    with TestClient(app, client=("127.0.0.1", 50000)) as http:
        workspace_path = tmp_path / "work"
        workspace_path.mkdir()
        workspace = http.post("/desktop/api/workspaces", headers=SESSION, json={"path": str(workspace_path)}).json()
        task = http.post(f"/desktop/api/workspaces/{workspace['workspace_id']}/threads", headers=SESSION, json={"thread_id": "wb-" + uuid.uuid4().hex, "title": "workbench"}).json()
        service = app.state.desktop_service
        model = service.app_config.get_model("deepseek-v4-flash")
        model.use = "focus.models.responses:FocusResponsesChatModel"
        model.provider, model.protocol = "deepseek", "responses"
        yield http, task, workspace_path, requests


def open_draft(http, task):
    response = http.post(f"/desktop/api/tasks/{task['task_id']}/drafts/open", headers=SESSION)
    assert response.status_code == 200, response.text
    return response.json()


def save(http, draft, document, **extra):
    from backend.app.desktop.session_patrol.contracts import AuthoringDocument
    document = {**document, "schema_version": 2}
    document = AuthoringDocument.model_validate(document).model_dump(mode="json")
    return http.put(f"/desktop/api/drafts/{draft['draft_id']}", headers=SESSION, json={
        "authoring_document": document, "draft_revision": draft["draft_revision"],
        "equipment": {"model_name": "deepseek-v4-flash", "permissions": ["read"], "skills": []}, **extra})


def wait_run(http, run_id, *, ignore_interrupted=False):
    import time
    for _ in range(200):
        payload = http.get(f"/desktop/api/runs/{run_id}", headers=SESSION).json()
        if payload["status"] in {"success", "error", "interrupted"}:
            if ignore_interrupted and payload["status"] == "interrupted":
                time.sleep(.025)
                continue
            assert payload["status"] == "success", payload
            return payload
        time.sleep(.025)
    raise AssertionError(f"Run timeout: {payload}")


def test_free_save_cas_raw_buffer_and_mode_roundtrip(client):
    http, task, _, _ = client
    draft = open_draft(http, task)
    document = {"schema_version": 2, "entries": [{"entry_id": "e1", "role": "tool", "content": [{"type": "future"}], "tool_call_id": "orphan"}], "raw_buffer": "{invalid", "raw_error": "JSON error", "unknown": {"future": True}}
    saved = save(http, draft, document)
    assert saved.status_code == 200, saved.text
    value = saved.json()
    assert value["authoring_document"]["raw_buffer"] == "{invalid"
    reloaded = http.get(f"/desktop/api/drafts/{draft['draft_id']}", headers=SESSION)
    assert reloaded.status_code == 200
    assert reloaded.json()["draft_revision"] == value["draft_revision"]
    assert reloaded.json()["authoring_document"] == value["authoring_document"]
    assert save(http, draft, document).status_code == 409
    old_client = http.put(f"/desktop/api/drafts/{draft['draft_id']}", headers=SESSION, json={"history_messages": [], "final_human_message": "overwrite"})
    assert old_client.status_code == 409
    preview = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION)
    assert preview.status_code == 200 and not preview.json()["executable"]
    assert open_draft(http, task)["authoring_document"]["unknown"] == {"future": True}
    from backend.tests.test_context_patrol_service import _seed_root
    http.portal.call(_seed_root, app.state.desktop_service, task["thread_id"], "稳定根任务")
    curator = save(http, value, document, mode="context_curator", curation_policy={})
    assert curator.status_code == 200, curator.text
    assert curator.json()["authoring_document"] == value["authoring_document"]
    standard = save(http, curator.json(), document, mode="standard")
    assert standard.status_code == 200 and standard.json()["authoring_document"] == value["authoring_document"]


def test_actual_graph_empty_tools_history_restart_and_idempotency(client):
    http, task, _, requests = client
    draft = open_draft(http, task)
    doc = {"schema_version": 2, "instructions": "基础行为", "entries": [
        {"entry_id": "s", "role": "system", "content": "有序指令"},
        {"entry_id": "a", "role": "assistant", "content": "", "tool_calls": [{"id": "example", "name": "read_file", "args": {"path": "does-not-exist.txt"}}]},
        {"entry_id": "t", "role": "tool", "content": "模拟成功", "tool_call_id": "example"}]}
    saved = save(http, draft, doc); assert saved.status_code == 200, saved.text
    preview = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION)
    assert preview.status_code == 200, preview.text
    plan = preview.json(); assert plan["executable"], plan
    key = uuid.uuid4().hex
    deploy = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": key, "preview_token": plan["preview_token"]})
    assert deploy.status_code == 200, deploy.text
    run = deploy.json(); settled = wait_run(http, run["run_id"])
    assert settled["execution_thread_id"] != task["thread_id"]
    again = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": key})
    assert again.json()["run_id"] == run["run_id"]
    assert len(requests) == 1
    assert requests[0] == plan["request"]
    async def audit():
        service = app.state.desktop_service
        async with service.session_factory() as session:
            definition = await session.scalar(select(PatrolDeploymentDefinition).where(PatrolDeploymentDefinition.run_id == run["run_id"]))
            count = await session.scalar(text("SELECT count(*) FROM tool_execution_attempts WHERE run_id=:r"), {"r": run["run_id"]})
            assert count == 0
            assert definition.document["entries"][-1]["payload"]["output"] == "模拟成功"
    http.portal.call(audit)
    retried = http.post(f"/desktop/api/agents/{run['agent_id']}/retry", headers=SESSION)
    assert retried.status_code == 200, retried.text
    second = wait_run(http, retried.json()["run_id"])
    assert second["agent_id"] == run["agent_id"] and second["execution_thread_id"] != settled["execution_thread_id"]
    assert len(requests) == 2
    continued = http.post(f"/desktop/api/agents/{run['agent_id']}/continue", headers=SESSION,
        json={"message": "从旧版本继续", "run_id": run["run_id"], "checkpoint_id": settled["final_checkpoint_id"]})
    assert continued.status_code == 200, continued.text
    third = wait_run(http, continued.json()["run_id"])
    assert third["execution_thread_id"] == settled["execution_thread_id"]
    assert requests[-1]["input"][-1]["content"] == "从旧版本继续" or any(i.get("content") == "从旧版本继续" for i in requests[-1]["input"])


def test_file_freeze_and_stale_preview(client):
    http, task, path, _ = client
    draft = open_draft(http, task)
    saved = save(http, draft, {"schema_version": 2, "entries": []}); assert saved.status_code == 200
    path.joinpath("evidence.txt").write_text("冻结第一版", encoding="utf-8")
    imported = http.post(f"/desktop/api/drafts/{draft['draft_id']}/sources", headers=SESSION,
        json={"kind": "file", "context_id": task["task_id"], "path": "evidence.txt", "draft_revision": saved.json()["draft_revision"]})
    assert imported.status_code == 200, imported.text
    path.joinpath("evidence.txt").unlink()
    assert open_draft(http, task)["authoring_document"]["entries"][0]["payload"]["content"] == "冻结第一版"
    missing = http.post(f"/desktop/api/drafts/{draft['draft_id']}/sources", headers=SESSION,
        json={"kind": "file", "context_id": task["task_id"], "path": "evidence.txt", "draft_revision": imported.json()["draft_revision"]})
    assert missing.status_code == 422 and missing.json()["detail"]["code"] == "source_unavailable"
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    doc = imported.json()["authoring_document"]; doc["entries"][0]["payload"]["content"] = "用户改写"
    updated = save(http, imported.json(), doc); assert updated.status_code == 200
    stale = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "refresh_required"


def test_database_definition_immutable_and_precommit_rollback(client, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    http, task, _, _ = client
    draft = open_draft(http, task)
    saved = save(http, draft, {"schema_version": 2, "entries": []}); assert saved.status_code == 200
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    key = uuid.uuid4().hex
    def fail(session, flush_context, instances):
        if any(isinstance(value, PatrolDeploymentDefinition) for value in session.new):
            raise RuntimeError("precommit fault")
    event.listen(Session, "before_flush", fail)
    try:
        with pytest.raises(RuntimeError, match="precommit fault"):
            http.portal.call(app.state.desktop_service.session_patrol.deploy, draft["draft_id"], key, plan["preview_token"])
    finally:
        event.remove(Session, "before_flush", fail)
    async def no_half_deployment():
        async with app.state.desktop_service.session_factory() as session:
            assert await session.scalar(select(DesktopRun).where(DesktopRun.deployment_id == key)) is None
            assert (await session.get(PatrolDraft, draft["draft_id"])).status == "editing"
    http.portal.call(no_half_deployment)
    response = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": key, "preview_token": plan["preview_token"]})
    assert response.status_code == 200, response.text
    wait_run(http, response.json()["run_id"])
    async def immutable():
        service = app.state.desktop_service
        async with service.session_factory() as session:
            row = await session.scalar(select(PatrolDeploymentDefinition).where(PatrolDeploymentDefinition.run_id == response.json()["run_id"]))
            original = row.document_hash
            with pytest.raises(Exception, match="immutable"):
                await session.execute(text("UPDATE patrol_deployment_definitions SET document_hash='changed' WHERE definition_id=:id"), {"id": row.definition_id})
            await session.rollback()
        async with service.session_factory() as session:
            row = await session.scalar(select(PatrolDeploymentDefinition).where(PatrolDeploymentDefinition.run_id == response.json()["run_id"]))
            assert row.document_hash == original
            from importlib import import_module
            migration = import_module("focus.persistence.migrations.versions.c36f7a8b9c0d_patrol_authoring")
            def protected_downgrade(sync_session):
                with monkeypatch.context() as scoped:
                    scoped.setattr(migration.op, "get_bind", lambda: sync_session.connection())
                    with pytest.raises(RuntimeError, match="已有部署定义"):
                        migration.downgrade()
            await session.run_sync(protected_downgrade)
    http.portal.call(immutable)


def test_complete_budget_and_empty_deployment(client):
    http, task, _, requests = client
    draft = open_draft(http, task)
    saved = save(http, draft, {"schema_version": 2, "entries": []}); assert saved.status_code == 200
    model = app.state.desktop_service.app_config.get_model("deepseek-v4-flash")
    before = model.context_window
    baseline = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    model.context_window = baseline["budget"]["total"] - baseline["budget"]["tools"] + 10
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    assert plan["budget"]["tools"] > 10 and plan["budget"]["total"] > model.context_window
    assert plan["budget"]["total"] - plan["budget"]["tools"] < model.context_window
    assert "window_exceeded" in {d["code"] for d in plan["diagnostics"]}
    model.context_window = before
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    assert plan["executable"]
    response = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    assert response.status_code == 200, response.text
    wait_run(http, response.json()["run_id"])
    assert len(requests) == 1


def test_resume_uses_original_run_and_exact_interrupted_graph(client, monkeypatch):
    from langchain.agents.middleware import AgentMiddleware
    from langgraph.types import interrupt
    import backend.app.desktop.service as service_module
    original = service_module.make_lead_agent
    class PauseBeforeSampling(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            interrupt({"type": "patrol_test_review"})
            return None
    async def factory(**kwargs):
        kwargs["additional_middlewares"] = [*kwargs.get("additional_middlewares", []), PauseBeforeSampling()]
        return await original(**kwargs)
    monkeypatch.setattr(service_module, "make_lead_agent", factory)
    http, task, _, requests = client
    draft = open_draft(http, task)
    assert save(http, draft, {"schema_version": 2, "entries": []}).status_code == 200
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    deployed = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    assert deployed.status_code == 200, deployed.text
    run = deployed.json()
    import time
    for _ in range(200):
        current = http.get(f"/desktop/api/runs/{run['run_id']}", headers=SESSION).json()
        if current["status"] == "interrupted":
            break
        time.sleep(.025)
    assert current["status"] == "interrupted", current
    assert len(requests) == 0
    resumed = http.post(f"/desktop/api/runs/{run['run_id']}/patrol-resume", headers=SESSION, json={"resume": {"approved": True}})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["run_id"] == run["run_id"]
    settled = wait_run(http, run["run_id"], ignore_interrupted=True)
    assert settled["execution_thread_id"] == run["execution_thread_id"]
    assert settled["checkpoint_ns"] == run["checkpoint_ns"] and len(requests) == 1


def test_source_versions_runtime_images_and_path_boundary(client):
    from datetime import UTC, datetime
    from langchain_core.messages import HumanMessage, SystemMessage
    from backend.app.desktop.context_evolution import ContextRevisionRepository, ContextRevisionContract, ContextRevisionRef
    from focus.history import serialize_history_message, content_hash
    http, task, path, requests = client
    repository = ContextRevisionRepository()
    async def seed(context, label, generation):
        service = app.state.desktop_service
        graph = await service.contexts._make_state_graph()
        config = await graph.aupdate_state({"configurable": {"thread_id": context["thread_id"], "checkpoint_ns": ""}},
            {"messages": [HumanMessage(content=label, id=label), SystemMessage(content="旧运行权限", id="runtime-"+label, additional_kwargs={"focus_context": {"origin": "runtime", "kind": "world_state_update", "scope": "runtime"}})]}, as_node="model")
        state = await graph.aget_state(config)
        records = tuple(serialize_history_message(m) for m in state.values["messages"])
        ref = ContextRevisionRef(context_id=context["task_id"], revision_id=uuid.uuid4().hex, generation=generation,
            execution_thread_id=context["thread_id"], checkpoint_ns="", checkpoint_id=config["configurable"]["checkpoint_id"], payload_mode="checkpoint")
        async with service.session_factory() as session:
            await repository.insert(session, ContextRevisionContract(ref=ref, execution_messages=records, content_hash=content_hash(records), projection_status="valid", origin_kind="root", created_at=datetime.now(UTC)))
            await session.commit()
        return ref
    second = http.post(f"/desktop/api/workspaces/{task['workspace_id']}/threads", headers=SESSION, json={"thread_id": "source-"+uuid.uuid4().hex, "title": "第二来源"}).json()
    first_ref = http.portal.call(seed, task, "第一版本", 20)
    second_ref = http.portal.call(seed, second, "第二来源", 20)
    draft = open_draft(http, task)
    draft = save(http, draft, {"schema_version": 2, "entries": []}).json()
    for source in (first_ref, second_ref):
        response = http.post(f"/desktop/api/drafts/{draft['draft_id']}/sources", headers=SESSION,
            json={"kind": "context", "context_id": source.context_id, "revision_id": source.revision_id, "draft_revision": draft["draft_revision"]})
        assert response.status_code == 200, response.text
        draft = response.json()
    assert [e["payload"]["content"] for e in draft["authoring_document"]["entries"]] == ["第一版本", "第二来源"]
    http.portal.call(seed, task, "父会话前进", 21)
    historical = http.post(f"/desktop/api/drafts/{draft['draft_id']}/sources", headers=SESSION,
        json={"kind": "context", "revision_id": first_ref.revision_id, "include_historical_runtime": True, "message_ids": ["runtime-第一版本"], "draft_revision": draft["draft_revision"]})
    assert historical.status_code == 200, historical.text
    draft = historical.json()
    assert draft["authoring_document"]["entries"][-1]["reference_only"]
    forbidden = http.post(f"/desktop/api/drafts/{draft['draft_id']}/sources", headers=SESSION,
        json={"kind": "file", "context_id": task["task_id"], "path": "../outside.txt", "draft_revision": draft["draft_revision"]})
    assert forbidden.status_code in {403, 422}, forbidden.text
    import base64
    path.joinpath("image.png").write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/WuoAAAAASUVORK5CYII="))
    image = http.post(f"/desktop/api/drafts/{draft['draft_id']}/sources", headers=SESSION,
        json={"kind": "file", "context_id": task["task_id"], "path": "image.png", "draft_revision": draft["draft_revision"]})
    assert image.status_code == 200, image.text
    draft = image.json(); path.joinpath("image.png").unlink()
    block = draft["authoring_document"]["entries"][-1]["payload"]["content"][0]
    assert block["image_url"].startswith("data:image/png;base64,")
    app.state.desktop_service.app_config.get_model("deepseek-v4-flash").supports_image_input = True
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    assert plan["executable"], plan["diagnostics"]
    deployment = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    assert deployment.status_code == 200, deployment.text
    wait_run(http, deployment.json()["run_id"])
    assert requests[-1] == plan["request"]
    audit = http.get(f"/desktop/api/runs/{deployment.json()['run_id']}/patrol-definition", headers=SESSION)
    assert audit.status_code == 200 and audit.json()["execution"]["thread_id"] != task["thread_id"]
    assert all("record" not in source for source in audit.json()["sources"].values())
    async def source_audit():
        async with app.state.desktop_service.session_factory() as session:
            definition = await session.scalar(select(PatrolDeploymentDefinition).where(PatrolDeploymentDefinition.run_id == deployment.json()["run_id"]))
            refs = [s["source"] for s in definition.sources.values()]
            assert any(r.get("revision_id") == first_ref.revision_id and r["checkpoint_id"] == first_ref.checkpoint_id for r in refs)
    http.portal.call(source_audit)


def test_preview_equipment_binding_and_definition_edit(client, monkeypatch):
    http, task, _, requests = client
    draft = open_draft(http, task)
    draft = save(http, draft, {"schema_version": 2, "entries": [{"entry_id": "x", "role": "user", "content": "目标"}]}).json()
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    model = app.state.desktop_service.app_config.get_model("deepseek-v4-flash")
    model.model = model.model + "-changed"
    stale = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "refresh_required"
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    key = uuid.uuid4().hex
    service = app.state.desktop_service
    notify = service.notify_run_dispatch
    monkeypatch.setattr(service, "notify_run_dispatch", lambda: (_ for _ in ()).throw(RuntimeError("postcommit lost response")))
    with pytest.raises(RuntimeError, match="postcommit lost"):
        http.portal.call(service.session_patrol.deploy, draft["draft_id"], key, plan["preview_token"])
    monkeypatch.setattr(service, "notify_run_dispatch", notify)
    recovered = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": key})
    assert recovered.status_code == 200, recovered.text
    original = wait_run(http, recovered.json()["run_id"])
    edited = http.post(f"/desktop/api/agents/{original['agent_id']}/drafts/open", headers=SESSION)
    assert edited.status_code == 200, edited.text
    draft2 = edited.json(); doc = draft2["authoring_document"]; doc["instructions"] = "新的基础行为"
    draft2 = save(http, draft2, doc).json()
    collision = http.post(f"/desktop/api/drafts/{draft2['draft_id']}/deploy", headers=SESSION, json={"deployment_id": key})
    assert collision.status_code == 409 and collision.json()["detail"]["code"] == "deployment_key_conflict"
    model.provider = "openai"
    plan2 = http.post(f"/desktop/api/drafts/{draft2['draft_id']}/preview", headers=SESSION).json()
    assert plan2["executable"] and plan2["target"]["provider"] == "openai"
    deployed = http.post(f"/desktop/api/drafts/{draft2['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan2["preview_token"]})
    assert deployed.status_code == 200, deployed.text
    new = wait_run(http, deployed.json()["run_id"])
    assert new["execution_thread_id"] != original["execution_thread_id"]
    assert requests[-1] == plan2["request"] and len(requests) == 2
    blocked = http.post(f"/desktop/api/agents/{original['agent_id']}/continue", headers=SESSION,
        json={"message": "不能偷换原模型", "run_id": original["run_id"], "checkpoint_id": original["final_checkpoint_id"]})
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "restart_required"


def test_new_model_tool_call_executes_once_after_simulated_history(client, monkeypatch):
    import json
    from pathlib import Path
    from langchain_core.outputs import ChatGenerationChunk
    http, task, path, requests = client
    path.joinpath("actual.txt").write_text("真实工具证据", encoding="utf-8")
    effects = []
    original_read = Path.read_bytes
    def spy(target):
        if target.name == "actual.txt":
            effects.append(str(target))
        return original_read(target)
    monkeypatch.setattr(Path, "read_bytes", spy)
    async def stream(model, messages, stop=None, run_manager=None, **kwargs):
        requests.append(model.request_payload(messages, **kwargs))
        if len(requests) == 1:
            chunk = AIMessageChunk(content="", tool_call_chunks=[{"name": "read_file", "args": json.dumps({"path": "actual.txt"}), "id": "new-real-call", "index": 0}])
        else:
            chunk = AIMessageChunk(content="完成真实读取")
        yield ChatGenerationChunk(message=chunk)
    monkeypatch.setattr(FocusResponsesChatModel, "_astream", stream)
    draft = open_draft(http, task)
    doc = {"schema_version": 2, "entries": [
        {"entry_id": "call", "role": "assistant", "content": "", "tool_calls": [{"id": "simulated", "name": "read_file", "args": {"path": "not-executed.txt"}}]},
        {"entry_id": "result", "role": "tool", "content": "模拟输出", "tool_call_id": "simulated"}]}
    assert save(http, draft, doc).status_code == 200
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    assert plan["executable"]
    deployed = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    assert deployed.status_code == 200, deployed.text
    wait_run(http, deployed.json()["run_id"])
    assert len(requests) == 2 and len(effects) == 1
    assert any(i.get("call_id") == "new-real-call" and "真实工具证据" in str(i.get("output")) for i in requests[1]["input"])
    async def ledger():
        async with app.state.desktop_service.session_factory() as session:
            rows = list((await session.execute(text("SELECT call_id,status FROM tool_execution_attempts WHERE run_id=:r"), {"r": deployed.json()["run_id"]})).all())
            assert len(rows) == 1 and rows[0].call_id == "new-real-call"
    http.portal.call(ledger)


def test_startup_configuration_change_does_not_sample(client, monkeypatch):
    http, task, _, requests = client
    service = app.state.desktop_service
    original = service._build_agent_factory
    gate = http.portal.call(asyncio.Event)
    def build(*args, **kwargs):
        factory = original(*args, **kwargs)
        observer = kwargs.get("preparation_observer")
        if getattr(observer, "__name__", "") != "verify":
            return factory
        async def paused():
            await gate.wait()
            return await factory()
        return paused
    monkeypatch.setattr(service, "_build_agent_factory", build)
    draft = open_draft(http, task)
    assert save(http, draft, {"schema_version": 2, "entries": []}).status_code == 200
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    deployed = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    assert deployed.status_code == 200
    model = service.app_config.get_model("deepseek-v4-flash")
    model.model = model.model + "-changed"
    http.portal.call(gate.set)
    import time
    for _ in range(200):
        run = http.get(f"/desktop/api/runs/{deployed.json()['run_id']}", headers=SESSION).json()
        if run["status"] == "error":
            break
        time.sleep(.025)
    assert run["status"] == "error" and "refresh_required" in str(run), run
    assert not requests


def test_electron_workbench_with_actual_api_sources_and_deployment(client, tmp_path):
    import json
    import os
    import subprocess
    import threading
    import time
    from pathlib import Path
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    http, task, path, requests = client
    draft = open_draft(http, task)
    draft = save(http, draft, {"schema_version": 2, "entries": []}).json()
    path.joinpath("ui-source.txt").write_text("界面来源", encoding="utf-8")
    fault = {"save": False}
    class Bridge(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_OPTIONS(self):
            self._reply(200, {})
        def do_GET(self):
            self._forward()
        def do_POST(self):
            self._forward()
        def do_PUT(self):
            self._forward()
        def _reply(self, status, data):
            self.send_response(status)
            for key, value in {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,PUT,OPTIONS", "Access-Control-Allow-Headers": "content-type"}.items():
                self.send_header(key, value)
            self.end_headers(); self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
        def _forward(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/test/fail-next-save":
                fault["save"] = True; self._reply(200, {}); return
            if self.path == "/test/delete-source":
                path.joinpath("ui-source.txt").unlink(); self._reply(200, {}); return
            if self.command == "PUT":
                time.sleep(.06)
                if fault["save"]:
                    fault["save"] = False; self._reply(503, {"detail": "test network failure"}); return
            response = http.request(self.command, self.path, headers=SESSION, json=json.loads(body) if body else None)
            self._reply(response.status_code, response.json())
    fixture = tmp_path / "electron-api-fixture.json"
    fixture.write_text(json.dumps({"draft": draft}, ensure_ascii=False), encoding="utf-8")
    bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
    thread = threading.Thread(target=bridge.serve_forever, daemon=True); thread.start()
    root = Path(__file__).resolve().parents[2]
    executable = root / "desktop/node_modules/electron/dist/electron.exe"
    if not executable.is_file():
        bridge.shutdown(); pytest.skip("Electron executable unavailable")
    env = dict(os.environ); env.pop("ELECTRON_RUN_AS_NODE", None)
    env.update(FOCUS_PATROL_TEST_URL=f"http://127.0.0.1:{bridge.server_port}", FOCUS_PATROL_TEST_FIXTURE=str(fixture))
    try:
        result = subprocess.run(["node", str(root / "desktop/node_modules/electron/cli.js"), str(root / "desktop/patrol-workbench-api.e2e.cjs")], cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert '"definition_edit":true' in result.stdout and len(requests) == 1
    finally:
        bridge.shutdown(); bridge.server_close(); thread.join(timeout=3)


def test_patrol_resume_keeps_uncertain_tool_ledger_and_never_repeats_effect(client, monkeypatch):
    import json
    import time
    from pathlib import Path
    from langchain_core.outputs import ChatGenerationChunk
    from langgraph.types import interrupt
    from backend.app.desktop.execution_attempts import ToolExecutionLedger
    http, task, path, requests = client
    path.joinpath("uncertain.txt").write_text("外部效果只发生一次", encoding="utf-8")
    effects = []
    original_read = Path.read_bytes
    def spy(target):
        if target.name == "uncertain.txt":
            effects.append(str(target))
        return original_read(target)
    monkeypatch.setattr(Path, "read_bytes", spy)
    async def stream(model, messages, stop=None, run_manager=None, **kwargs):
        requests.append(model.request_payload(messages, **kwargs))
        yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[{"name": "read_file", "args": json.dumps({"path": "uncertain.txt"}), "id": "uncertain-call", "index": 0}]))
    monkeypatch.setattr(FocusResponsesChatModel, "_astream", stream)
    original_complete = ToolExecutionLedger.complete
    async def pause(ledger, identity, result):
        interrupt({"type": "uncertain_tool_test"})
        await original_complete(ledger, identity, result)
    monkeypatch.setattr(ToolExecutionLedger, "complete", pause)
    draft = open_draft(http, task)
    assert save(http, draft, {"schema_version": 2, "entries": []}).status_code == 200
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    response = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    for _ in range(200):
        run = http.get(f"/desktop/api/runs/{run_id}", headers=SESSION).json()
        if run["status"] == "interrupted":
            break
        time.sleep(.025)
    assert run["status"] == "interrupted" and len(effects) == 1 and len(requests) == 1, run
    resumed = http.post(f"/desktop/api/runs/{run_id}/patrol-resume", headers=SESSION, json={"resume": {"continue": True}})
    assert resumed.status_code == 200 and resumed.json()["run_id"] == run_id
    for _ in range(200):
        run = http.get(f"/desktop/api/runs/{run_id}", headers=SESSION).json()
        if run["status"] == "error":
            break
        time.sleep(.025)
    assert run["status"] == "error" and "结果不确定" in str(run), run
    assert len(effects) == 1 and len(requests) == 1


def test_legacy_interrupt_keeps_original_namespace_without_new_definition(client, monkeypatch):
    import time
    from langchain.agents.middleware import AgentMiddleware
    from langgraph.types import interrupt
    from backend.app.desktop.models import PatrolAgent
    from backend.app.desktop.run_orchestration.models import RunDispatch
    import backend.app.desktop.service as service_module
    original = service_module.make_lead_agent
    class PauseLegacy(AgentMiddleware):
        async def abefore_model(self, state, runtime):
            interrupt({"type": "legacy_test"})
    async def factory(**kwargs):
        kwargs["additional_middlewares"] = [*kwargs.get("additional_middlewares", []), PauseLegacy()]
        return await original(**kwargs)
    monkeypatch.setattr(service_module, "make_lead_agent", factory)
    http, task, path, requests = client
    identity = uuid.uuid4().hex; namespace = "legacy-patrol:"+identity
    async def seed():
        service = app.state.desktop_service
        equipment = {"model_name": "deepseek-v4-flash", "permissions": ["read"], "skills": [], "_durable_dispatch_execution": {"agent_role": "patrol", "base_prompt": "旧行为", "checkpoint_id": None}}
        async with service.session_factory() as session:
            session.add(PatrolAgent(agent_id=identity, task_id=task["task_id"], checkpoint_ns=namespace, system_prompt="旧行为", frozen_messages=[], equipment=equipment, mode="standard"))
            session.add(DesktopRun(run_id=identity, task_id=task["task_id"], agent_id=identity, kind="patrol", status="pending", input_messages=[], equipment=equipment, model_name="deepseek-v4-flash", origin="direct_user", checkpoint_ns=namespace, workspace_anchor={"workspace_id": task["workspace_id"], "workspace_path": str(path)}))
            await session.flush(); session.add(RunDispatch(dispatch_id=uuid.uuid4().hex, run_id=identity, status="accepted")); await session.commit()
        service.notify_run_dispatch()
    http.portal.call(seed)
    for _ in range(200):
        run = http.get(f"/desktop/api/runs/{identity}", headers=SESSION).json()
        if run["status"] == "interrupted":
            break
        time.sleep(.025)
    assert run["status"] == "interrupted" and not requests, run
    response = http.post(f"/desktop/api/runs/{identity}/patrol-resume", headers=SESSION, json={"resume": {"decision": "continue"}})
    assert response.status_code == 200, response.text
    settled = wait_run(http, identity, ignore_interrupted=True)
    assert settled["execution_thread_id"] == task["thread_id"] and settled["checkpoint_ns"] == namespace
    async def unchanged():
        async with app.state.desktop_service.session_factory() as session:
            assert await session.scalar(select(PatrolDeploymentDefinition).where(PatrolDeploymentDefinition.run_id == identity)) is None
            agent = await session.get(PatrolAgent, identity)
            assert agent.system_prompt == "旧行为" and agent.frozen_messages == [] and agent.checkpoint_ns == namespace
    http.portal.call(unchanged)


def test_material_version_is_frozen_and_missing_object_is_diagnostic(client, monkeypatch):
    http, task, path, _ = client
    material_path = path / "material.txt"
    material_path.write_text("材料历史第一版", encoding="utf-8")
    response = http.post(f"/desktop/api/tasks/{task['task_id']}/materials", headers=SESSION,
        json={"path": str(material_path), "retention": "irreplaceable", "confirm_git_init": True})
    assert response.status_code == 200, response.text
    material = response.json()
    versions = http.get(f"/desktop/api/materials/{material['material_id']}/versions", headers=SESSION).json()
    assert versions
    material_path.write_text("当前文件新版本", encoding="utf-8")
    draft = open_draft(http, task)
    draft = save(http, draft, {"schema_version": 2, "entries": []}).json()
    imported = http.post(f"/desktop/api/drafts/{draft['draft_id']}/sources", headers=SESSION,
        json={"kind": "material", "context_id": task["task_id"], "material_id": material["material_id"], "version_id": versions[-1]["version_id"], "draft_revision": draft["draft_revision"]})
    assert imported.status_code == 200, imported.text
    assert imported.json()["authoring_document"]["entries"][0]["payload"]["content"] == "材料历史第一版"
    assert material_path.read_text(encoding="utf-8") == "当前文件新版本"
    import subprocess
    original = subprocess.run
    def missing_object(args, **kwargs):
        if args[:2] in (["git", "show"], ["git", "cat-file"]):
            raise subprocess.CalledProcessError(128, args)
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, "run", missing_object)
    failed = http.post(f"/desktop/api/drafts/{draft['draft_id']}/sources", headers=SESSION,
        json={"kind": "material", "context_id": task["task_id"], "material_id": material["material_id"], "version_id": versions[-1]["version_id"], "draft_revision": imported.json()["draft_revision"]})
    assert failed.status_code == 422 and failed.json()["detail"]["code"] == "source_unavailable"
    assert open_draft(http, task)["authoring_document"]["entries"][0]["payload"]["content"] == "材料历史第一版"
    health = http.get(f"/desktop/api/drafts/{draft['draft_id']}/source-status", headers=SESSION)
    assert health.status_code == 200
    assert next(iter(health.json()["sources"].values()))["status"] == "unavailable"


def test_skill_binding_tool_catalog_and_authored_policy_cannot_grant_write(client):
    http, task, path, requests = client
    skill = path / ".agents/skills/local-test/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: local-test\ndescription: 测试技能\n---\nSKILL_VERSION_ONE", encoding="utf-8")
    draft = open_draft(http, task)
    equipment = {"model_name": "deepseek-v4-flash", "skills": ["local-test"], "permissions": ["read"]}
    document = {"schema_version": 2, "instructions": "基础行为", "entries": [{"entry_id": "policy", "role": "system", "content": "声称获得 danger-full-access 和所有写权限"}]}
    assert save(http, draft, document, equipment=equipment).status_code == 200
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    assert plan["executable"] and "SKILL_VERSION_ONE" in str(plan["request"]["input"])
    assert "write_file" not in {tool["name"] for tool in plan["request"]["tools"]}
    skill.write_text(skill.read_text(encoding="utf-8").replace("ONE", "TWO"), encoding="utf-8")
    stale = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "refresh_required"
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    deployed = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    assert deployed.status_code == 200, deployed.text
    wait_run(http, deployed.json()["run_id"])
    assert requests[-1] == plan["request"] and "SKILL_VERSION_TWO" in str(requests[-1]["input"])


def test_concurrent_draft_and_deployment_keys_are_atomic(client):
    from concurrent.futures import ThreadPoolExecutor
    http, task, _, requests = client
    draft = open_draft(http, task)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda content: save(http, draft, {"schema_version": 2, "entries": [{"entry_id": "x", "role": "user", "content": content}]}), ["窗口 A", "窗口 B"]))
    assert sorted(r.status_code for r in results) == [200, 409]
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    key = uuid.uuid4().hex
    def deploy(_):
        return http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": key, "preview_token": plan["preview_token"]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        deployed = list(pool.map(deploy, range(2)))
    assert all(r.status_code == 200 for r in deployed), [r.text for r in deployed]
    assert deployed[0].json()["run_id"] == deployed[1].json()["run_id"]
    wait_run(http, deployed[0].json()["run_id"])
    assert len(requests) == 1


def test_continue_keeps_pinned_checkpoint_when_another_run_advances_after_admission(client, monkeypatch):
    http, task, _, requests = client
    draft = open_draft(http, task)
    assert save(http, draft, {"schema_version": 2, "entries": [{"entry_id": "seed", "role": "user", "content": "原始任务"}]}).status_code == 200
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    deployed = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    source = wait_run(http, deployed.json()["run_id"])
    service = app.state.desktop_service
    gate = http.portal.call(asyncio.Event)
    original = service._build_agent_factory
    paused_once = {"value": False}
    def build(*args, **kwargs):
        factory = original(*args, **kwargs)
        equipment = args[3]
        if equipment.get("_durable_dispatch_execution", {}).get("execution_mode") != "continue" or paused_once["value"]:
            return factory
        paused_once["value"] = True
        async def paused():
            await gate.wait()
            return await factory()
        return paused
    monkeypatch.setattr(service, "_build_agent_factory", build)
    branch = {"run_id": source["run_id"], "checkpoint_id": source["final_checkpoint_id"]}
    first = http.post(f"/desktop/api/agents/{source['agent_id']}/continue", headers=SESSION, json={**branch, "message": "PINNED_OLD"})
    assert first.status_code == 200, first.text
    import time
    for _ in range(100):
        if paused_once["value"]:
            break
        time.sleep(.025)
    assert paused_once["value"]
    second = http.post(f"/desktop/api/agents/{source['agent_id']}/continue", headers=SESSION, json={**branch, "message": "HEAD_ADVANCE"})
    assert second.status_code == 200, second.text
    advanced = wait_run(http, second.json()["run_id"])
    assert advanced["final_checkpoint_id"] != branch["checkpoint_id"]
    http.portal.call(gate.set)
    wait_run(http, first.json()["run_id"])
    assert "PINNED_OLD" in str(requests[-1]["input"]) and "HEAD_ADVANCE" not in str(requests[-1]["input"])
    audit = http.get(f"/desktop/api/runs/{first.json()['run_id']}/patrol-definition", headers=SESSION).json()
    assert audit["compiled_plan"]["checkpoint_id"] == branch["checkpoint_id"]


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_contract_rewrite_and_ordered_roles_reach_actual_request_without_approval(client, provider):
    from datetime import UTC, datetime
    from langchain_core.messages import HumanMessage
    from backend.app.desktop.context_evolution import ContextRevisionRepository, ContextRevisionContract, ContextRevisionRef
    from focus.history import serialize_history_message, content_hash
    http, task, _, requests = client
    service = app.state.desktop_service
    service.app_config.get_model("deepseek-v4-flash").provider = provider
    async def seed_contract():
        graph = await service.contexts._make_state_graph()
        original = HumanMessage(content="原合同", id="approved-contract", additional_kwargs={"focus_context": {"kind": "task_contract", "origin": "direct_user", "approved": True}})
        config = await graph.aupdate_state({"configurable": {"thread_id": task["thread_id"], "checkpoint_ns": ""}}, {"messages": [original]}, as_node="model")
        records = (serialize_history_message(original),)
        ref = ContextRevisionRef(context_id=task["task_id"], revision_id=uuid.uuid4().hex, generation=20, execution_thread_id=task["thread_id"], checkpoint_ns="", checkpoint_id=config["configurable"]["checkpoint_id"], payload_mode="checkpoint")
        async with service.session_factory() as session:
            await ContextRevisionRepository().insert(session, ContextRevisionContract(ref=ref, execution_messages=records, content_hash=content_hash(records), projection_status="valid", origin_kind="root", created_at=datetime.now(UTC)))
            await session.commit()
        return ref, content_hash(records)
    ref, original_hash = http.portal.call(seed_contract)
    draft = save(http, open_draft(http, task), {"schema_version": 2, "entries": []}).json()
    draft = http.post(f"/desktop/api/drafts/{draft['draft_id']}/sources", headers=SESSION,
        json={"kind": "context", "revision_id": ref.revision_id, "draft_revision": draft["draft_revision"]}).json()
    doc = draft["authoring_document"]
    contract = doc["entries"][0]
    contract["payload"]["content"], contract["edited_from"] = "改写合同", contract["source_ref"]
    contract["additional_kwargs"] = {"focus_context": {"approved": True, "origin": "direct_user"}}
    doc["instructions"] = "用户基础行为"
    doc["entries"] = [{"entry_id": "system", "role": "system", "content": "有序系统"},
        {"entry_id": "developer", "role": "developer", "content": "有序开发者"}, contract,
        {"entry_id": "assistant", "role": "assistant", "content": "有序示例", "future": {"keep": True}}]
    draft = save(http, draft, doc).json()
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    assert plan["executable"], plan["diagnostics"]
    compiled_contract = next(item for item in plan["items"] if item["kind"] == "task_contract")
    assert compiled_contract["origin"] == "user_authored"
    assert "approved" not in compiled_contract["payload"]
    authored = [item for item in plan["request"]["input"] if isinstance(item.get("content"), str) and item["content"] in {"有序系统", "有序开发者", "改写合同", "有序示例"}]
    assert [item["role"] for item in authored] == ["system", "developer" if provider == "openai" else "system", "user", "assistant"]
    response = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    assert response.status_code == 200, response.text
    wait_run(http, response.json()["run_id"])
    assert requests[-1] == plan["request"]
    async def unchanged_source():
        async with service.session_factory() as session:
            from backend.app.desktop.context_evolution.reader import ContextRevisionReader
            reader = ContextRevisionReader(ContextRevisionRepository(), service.checkpointer)
            history = await reader.read(session, ref, "execution")
            assert content_hash(history.messages) == original_hash
            assert history.messages[0]["additional_kwargs"]["focus_context"]["approved"]
    http.portal.call(unchanged_source)


def test_explicit_non_success_placeholder_reaches_graph_without_tool_execution(client):
    http, task, _, requests = client
    draft = open_draft(http, task)
    doc = {"schema_version": 2, "entries": [{"entry_id": "pending", "role": "assistant", "content": "", "tool_calls": [{"id": "simulated", "name": "read_file", "args": {"path": "absent"}}]}]}
    draft = save(http, draft, doc).json()
    conflict = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    assert not conflict["executable"]
    doc["transformations"] = [{"document_hash": conflict["document_hash"], "entry_ids": ["pending"], "operation": "placeholder", "parameters": {}}]
    draft = save(http, draft, doc).json()
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    assert plan["executable"] and plan["messages"][-1]["status"] == "error"
    deployed = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION, json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]})
    assert deployed.status_code == 200, deployed.text
    run = wait_run(http, deployed.json()["run_id"])
    assert len(requests) == 1 and requests[0] == plan["request"]
    output = next(item for item in requests[0]["input"] if item["type"] == "function_call_output")
    assert "未执行" in output["output"]
    async def no_execution_proof():
        async with app.state.desktop_service.session_factory() as session:
            assert await session.scalar(text("SELECT count(*) FROM tool_execution_attempts WHERE run_id=:r"), {"r": run["run_id"]}) == 0
    http.portal.call(no_execution_proof)
