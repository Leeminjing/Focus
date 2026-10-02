"""本文件提供 v3 typed Patrol 语义、桥接、旧草稿和 canonical 来源回归。

输入为独立消息、工具组和两个 Provider；输出为语义 identity、实际请求、执行隔离和读时适配断言。
工作流使用生产编译与真实投影，再通过隔离 API 验证定义/图/checkpoint 未登记模拟执行。
示例：pytest backend/tests/test_session_patrol_typed_authoring.py -q。
"""
from backend.tests.test_session_patrol_workbench_api import client as client
from copy import deepcopy
import uuid
import pytest
from backend.app.desktop.session_patrol.contracts import AuthoringDocument
from backend.app.desktop.session_patrol.compiler import compile_document
from backend.app.desktop.session_patrol.authoring_adapters import upgrade_document
from focus.history import FocusItem, items_to_messages, messages_to_items, semantic_policy, content_hash
from focus.models.provider_contract import ProviderContract
from focus.models.response_projection import ResponsesRequestProjector
from langchain_core.messages import SystemMessage


def typed_document():
    return {"schema_version": 3, "instructions": "基础行为", "entries": [
        {"entry_id": "d", "kind": "message", "payload": {"role": "developer", "content": "验证"}},
        {"entry_id": "u", "kind": "message", "payload": {"role": "user", "content": "任务"}},
        {"entry_id": "a", "kind": "message", "payload": {"role": "assistant", "content": "检查"}},
        {"entry_id": "c", "kind": "function_call", "payload": {"call_id": "read", "name": "read_file", "arguments": '{"path":"missing.txt"}'}},
        {"entry_id": "o", "kind": "function_call_output", "payload": {"call_id": "read", "output": "模拟结果"}},
        {"entry_id": "cc", "kind": "custom_tool_call", "payload": {"call_id": "patch", "name": "apply_patch", "input": "示范输入"}},
        {"entry_id": "co", "kind": "custom_tool_call_output", "payload": {"call_id": "patch", "output": "未执行"}},
        {"entry_id": "t", "kind": "task_contract", "payload": {"role": "user", "content": "任务合同"}},
        {"entry_id": "i", "kind": "agent_collaboration", "payload": {"author": "teammate", "recipient": "patrol", "content": "验证结果"}}]}


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_typed_items_remain_authority_across_projection_and_bridge(provider):
    raw = typed_document()
    document = AuthoringDocument.model_validate(raw)
    compiled = compile_document(document, provider=provider)
    assert compiled.executable, compiled.diagnostics
    parsed = tuple(FocusItem.model_validate(item) for item in compiled.items)
    assert messages_to_items(items_to_messages(parsed)) == parsed
    request = ResponsesRequestProjector(ProviderContract(provider, "responses")).build([SystemMessage("base"), *items_to_messages(parsed)], model="test")
    assert [item["type"] for item in request["input"]] == ["message"] * 3 + ["function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output", "message", "message"]
    assert request["input"][0]["role"] == ("developer" if provider == "openai" else "system")
    assert request["input"][3]["arguments"] == raw["entries"][3]["payload"]["arguments"]
    assert request["input"][4]["output"] == "模拟结果"
    assert not any(message.additional_kwargs.get("focus_response_items") for message in items_to_messages(parsed))
    assert all(item.origin == "user_authored" for item in parsed)
    assert semantic_policy(parsed[0]) == "exclude"
    assert semantic_policy(parsed[4]) == "evidence_only"
    assert document.model_dump()["entries"][3]["payload"] == raw["entries"][3]["payload"]
    changed = items_to_messages(parsed)
    changed[1].content = "未同步篡改"
    with pytest.raises(ValueError, match="正文不一致"):
        messages_to_items(changed)


def test_old_multi_calls_expand_without_writing_or_losing_original():
    raw = {"schema_version": 2, "entries": [{"entry_id": "a", "role": "ai", "content": "文本", "unknown": [1],
        "tool_calls": [{"id": "c1", "name": "read", "args": {}}, {"id": "c2", "name": "list", "args": {}}]},
        {"entry_id": "o1", "role": "tool", "tool_call_id": "c1", "content": "1"},
        {"entry_id": "o2", "role": "tool", "tool_call_id": "c2", "content": "2"}]}
    before = deepcopy(raw)
    document = AuthoringDocument.model_validate(raw)
    assert raw == before
    assert document.schema_version == 3 and len(document.entries) == 5
    assert document.entries[0].model_extra["legacy_record"] == before["entries"][0]
    assert document.entries[1].model_extra["source_group"] == "a"
    result = compile_document(document, provider="openai")
    assert result.executable and len(result.items) == 5
    assert content_hash(raw) == content_hash(before)
    assert upgrade_document(document.model_dump()) == document.model_dump()


@pytest.mark.parametrize("kind", ["future", "reasoning", "world_state_update"])
def test_unknown_and_runtime_authoring_saved_but_diagnosed(kind):
    document = AuthoringDocument.model_validate({"schema_version": 3, "entries": [{"entry_id": "unknown", "kind": kind,
        "payload": {"future": [1, {"kept": True}]}, "payload_buffer": "{unfinished"}]})
    before = document.model_dump()
    compiled = compile_document(document, provider="openai")
    assert not compiled.executable and compiled.diagnostics[0]["entry_ids"] == ["unknown"]
    assert document.model_dump() == before


@pytest.mark.usefixtures("isolated_postgres_database")
def test_typed_api_definition_graph_and_old_client_guard(client):
    from backend.tests.test_session_patrol_workbench_api import open_draft, save, wait_run
    from backend.app.gateway.app import app
    from backend.app.desktop.models import PatrolDeploymentDefinition
    from sqlalchemy import select, text
    http, task, _, requests = client
    draft = save(http, open_draft(http, task), typed_document()).json()
    assert draft["authoring_document"]["schema_version"] == 3
    old = http.put(f"/desktop/api/drafts/{draft['draft_id']}", headers={"X-Focus-Session": "focus-dev-session"},
                   json={"authoring_document": {"schema_version": 2, "entries": []}, "draft_revision": draft["draft_revision"]})
    assert old.status_code == 409 and old.json()["detail"]["code"] == "client_schema_conflict"
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers={"X-Focus-Session": "focus-dev-session"}).json()
    assert plan["executable"], plan["diagnostics"]
    run = http.post(f"/desktop/api/drafts/{draft['draft_id']}/deploy", headers={"X-Focus-Session": "focus-dev-session"},
        json={"deployment_id": uuid.uuid4().hex, "preview_token": plan["preview_token"]}).json()
    wait_run(http, run["run_id"])
    assert len(requests) == 1 and requests[0] == plan["request"]
    async def check():
        async with app.state.desktop_service.session_factory() as session:
            definition = await session.scalar(select(PatrolDeploymentDefinition).where(PatrolDeploymentDefinition.run_id == run["run_id"]))
            assert definition.document["schema_version"] == 3
            assert definition.compiled_plan["items"] == plan["items"]
            assert await session.scalar(text("SELECT count(*) FROM tool_execution_attempts WHERE run_id=:r"), {"r": run["run_id"]}) == 0
    http.portal.call(check)
    next_draft = open_draft(http, task)
    imported = http.post(f"/desktop/api/drafts/{next_draft['draft_id']}/sources", headers={"X-Focus-Session": "focus-dev-session"},
        json={"kind": "patrol", "run_id": run["run_id"], "draft_revision": next_draft["draft_revision"]})
    assert imported.status_code == 200, imported.text
    imported_entries = imported.json()["authoring_document"]["entries"]
    assert sum(entry["kind"] == "function_call" for entry in imported_entries) == 1
    assert sum(entry["kind"] == "custom_tool_call" for entry in imported_entries) == 1
    assert all(entry.get("source_item_id") for entry in imported_entries)
    assert not any(entry["kind"] in {"world_state_update", "reasoning", "authored_instruction"} for entry in imported_entries)


def test_canonical_source_filters_opaque_per_item_and_retains_native_identity():
    from backend.app.desktop.session_patrol.source_projection import freeze_authoring_sources
    records = [FocusItem(item_id="r", kind="reasoning", origin="provider", payload={"type":"reasoning","encrypted_content":"opaque"}),
               FocusItem(item_id="c", message_id="group", kind="function_call", origin="provider", payload={"type":"function_call","call_id":"c1","name":"read","arguments":"{}"}),
               FocusItem(item_id="o", kind="function_call_output", origin="tool", payload={"type":"function_call_output","call_id":"c1","output":"evidence"})]
    raw = [item.model_dump(mode="json") for item in records]
    before = deepcopy(raw)
    entries, sources = freeze_authoring_sources(raw, {"revision_id":"R1","generation":1})
    assert [entry.kind for entry in entries] == ["function_call", "function_call_output"]
    assert [entry.model_extra["source_item_id"] for entry in entries] == ["c", "o"]
    assert entries[0].model_extra["source_group"] == "group"
    assert sources[entries[0].source_ref]["record"] == raw[1]
    assert raw == before
    compiled = compile_document(AuthoringDocument(entries=entries), provider="openai", sources=sources)
    assert compiled.executable and compiled.items[0]["source_refs"][0]["relation"] == "reference"
