"""本文件对外提供会话 Patrol 核验问题的真实 API/graph 回归。

输入为既有隔离 PostgreSQL/模型 fixture、未知 JSON 字段、旧 Run 和冻结来源；输出为逐项诊断、原定义重启及状态只读断言。
工作流通过保存、preview、continue/restart、精确 checkpoint 和审计验证生产端口；不连接线上 Provider。
示例：pytest backend/tests/test_session_patrol_remediation.py -q。
"""
import uuid
import pytest
from backend.tests.test_session_patrol_workbench_api import client as client, save, open_draft, wait_run, SESSION
from backend.app.gateway.app import app
from backend.app.desktop.models import PatrolAgent, DesktopRun
from backend.app.desktop.run_orchestration.models import RunDispatch
from focus.history import content_hash, serialize_history_message

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


@pytest.mark.parametrize("kind", [{"future": True}, ["future"], "future", 3])
def test_saved_unknown_native_shapes_have_entry_diagnostics(client, kind):
    http, task, _, requests = client
    draft = open_draft(http, task)
    document = {"schema_version": 2, "entries": [{"entry_id": "native", "role": "user", "content": "KEEP", "type": kind}]}
    saved = save(http, draft, document)
    assert saved.status_code == 200
    preview = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION)
    assert preview.status_code == 200, preview.text
    assert not preview.json()["executable"]
    assert any(d["code"] == "unsupported_native_item" and d["entry_ids"] == ["native"] for d in preview.json()["diagnostics"])
    assert http.get(f"/desktop/api/drafts/{draft['draft_id']}", headers=SESSION).json()["authoring_document"]["entries"][0]["type"] == kind
    assert not requests


def test_invalid_structural_buffer_saves_beside_last_valid_document(client):
    http, task, _, requests = client
    draft = open_draft(http, task)
    document = {"schema_version": 2, "entries": [{"entry_id": "e", "role": "user", "content": "KEEP"}],
                "raw_buffer": '{"schema_version":2,"entries":[{"role":3}]}', "raw_error": "entries[0].role 需要字符串"}
    saved = save(http, draft, document)
    assert saved.status_code == 200
    reopened = http.get(f"/desktop/api/drafts/{draft['draft_id']}", headers=SESSION).json()
    assert reopened["authoring_document"]["entries"] == saved.json()["authoring_document"]["entries"]
    assert reopened["authoring_document"]["entries"][0]["content"] == "KEEP"
    assert reopened["authoring_document"]["raw_buffer"] == document["raw_buffer"]
    assert not http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()["executable"]
    assert not requests


def test_legacy_continue_edit_and_restart_restore_original_definition(client):
    http, task, path, requests = client
    identity = uuid.uuid4().hex
    original = [{"role": "human", "content": "ORIGINAL_TASK"}]
    namespace = "legacy:" + identity

    async def seed():
        service = app.state.desktop_service
        equipment = {"model_name": "deepseek-v4-flash", "permissions": ["read"], "skills": [],
                     "_durable_dispatch_execution": {"agent_role": "patrol", "base_prompt": "base", "checkpoint_id": None}}
        async with service.session_factory() as session:
            session.add(PatrolAgent(agent_id=identity, task_id=task["task_id"], checkpoint_ns=namespace,
                system_prompt="base", frozen_messages=original, equipment=equipment, mode="standard", curation_policy={}))
            session.add(DesktopRun(run_id=identity, task_id=task["task_id"], agent_id=identity, kind="patrol", status="pending",
                input_messages=original, equipment=equipment, model_name="deepseek-v4-flash", origin="direct_user", checkpoint_ns=namespace,
                workspace_anchor={"workspace_id": task["workspace_id"], "workspace_path": str(path)}))
            await session.flush()
            session.add(RunDispatch(dispatch_id=uuid.uuid4().hex, run_id=identity, status="accepted"))
            await session.commit()
        service.notify_run_dispatch()

    http.portal.call(seed)
    initial = wait_run(http, identity)
    config = {"configurable": {"thread_id": task["thread_id"], "checkpoint_ns": namespace, "checkpoint_id": initial["final_checkpoint_id"]}}

    async def original_checkpoint_hash():
        checkpoint = await app.state.desktop_service.checkpointer.aget_tuple(config)
        return content_hash([serialize_history_message(m) for m in checkpoint.checkpoint["channel_values"]["messages"]])

    checkpoint_hash = http.portal.call(original_checkpoint_hash)
    follow = http.post(f"/desktop/api/agents/{identity}/continue", headers=SESSION,
        json={"message": "FOLLOWUP_ONLY", "run_id": identity, "checkpoint_id": initial["final_checkpoint_id"]})
    assert follow.status_code == 200, follow.text
    settled = wait_run(http, follow.json()["run_id"])
    second = http.post(f"/desktop/api/agents/{identity}/continue", headers=SESSION,
        json={"message": "SECOND_FOLLOWUP", "run_id": settled["run_id"], "checkpoint_id": settled["final_checkpoint_id"]})
    assert second.status_code == 200, second.text
    wait_run(http, second.json()["run_id"])
    assert "ORIGINAL_TASK" in str(requests[-1]["input"]) and "FOLLOWUP_ONLY" in str(requests[-1]["input"])
    copied = http.post(f"/desktop/api/agents/{identity}/drafts/open", headers=SESSION)
    assert copied.status_code == 200, copied.text
    assert [e["content"] for e in copied.json()["authoring_document"]["entries"]] == ["ORIGINAL_TASK"]
    restarted = http.post(f"/desktop/api/agents/{identity}/retry", headers=SESSION)
    assert restarted.status_code == 200, restarted.text
    final = wait_run(http, restarted.json()["run_id"])
    assert final["execution_thread_id"] != task["thread_id"] and final["agent_id"] == identity
    assert "ORIGINAL_TASK" in str(requests[-1]["input"])
    assert "FOLLOWUP_ONLY" not in str(requests[-1]["input"]) and "SECOND_FOLLOWUP" not in str(requests[-1]["input"])
    assert not any(i.get("role") == "assistant" and i.get("content") == "完成" for i in requests[-1]["input"])
    assert http.portal.call(original_checkpoint_hash) == checkpoint_hash

    async def original_agent_hash():
        async with app.state.desktop_service.session_factory() as session:
            return content_hash((await session.get(PatrolAgent, identity)).frozen_messages)
    assert http.portal.call(original_agent_hash) == content_hash(original)


def test_frozen_source_health_changes_without_mutating_definition_or_content(client):
    http, task, path, requests = client
    draft = save(http, open_draft(http, task), {"schema_version": 2, "entries": []}).json()
    source_path = path / "frozen.txt"
    source_path.write_text("FROZEN_BODY", encoding="utf-8")
    imported = http.post(f"/desktop/api/drafts/{draft['draft_id']}/sources", headers=SESSION,
        json={"kind": "file", "context_id": task["task_id"], "path": "frozen.txt", "draft_revision": draft["draft_revision"]}).json()
    entry = imported["authoring_document"]["entries"][0]
    status_url = f"/desktop/api/drafts/{draft['draft_id']}/source-status"
    assert http.get(status_url, headers=SESSION).json()["sources"][entry["source_ref"]]["status"] == "available"
    source_path.write_text("CURRENT_DIFFERENT_BODY", encoding="utf-8")
    assert http.get(status_url, headers=SESSION).json()["sources"][entry["source_ref"]]["status"] == "available"
    preview = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    run = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers=SESSION,
        json={"deployment_id": uuid.uuid4().hex, "preview_token": preview["preview_token"]}).json()
    wait_run(http, run["run_id"])
    audit_url = f"/desktop/api/runs/{run['run_id']}/patrol-definition"
    original_audit = http.get(audit_url, headers=SESSION).json()
    source_path.unlink()
    assert http.get(status_url, headers=SESSION).json()["sources"][entry["source_ref"]]["status"] == "unavailable"
    audit = http.get(audit_url, headers=SESSION).json()
    assert audit["source_status"][entry["source_ref"]]["status"] == "unavailable"
    assert audit["document"] == original_audit["document"] and audit["sources"] == original_audit["sources"]
    assert "FROZEN_BODY" in str(requests[-1]["input"]) and "CURRENT_DIFFERENT_BODY" not in str(requests[-1]["input"])
    reopened = http.get(f"/desktop/api/drafts/{draft['draft_id']}", headers=SESSION).json()
    assert reopened["authoring_document"]["entries"][0] == entry
