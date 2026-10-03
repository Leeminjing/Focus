"""本文件对外提供精确来源预览、逐行插入及并发/快照一致性的行为验收。

输入为混合历史记录、隔离 API 草稿与工作区文件；输出为稳定行、完整正文、原子插入、可恢复冲突与准确诊断断言。
具体工作流为只读预览后按指纹/锚点插入，制造来源更新与并发请求并核对完整草稿和冻结引用。
示例：python -m pytest backend/tests/test_session_patrol_source_assembly.py -q。
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from backend.tests.test_session_patrol_workbench_api import client as client, open_draft, save, SESSION
from backend.app.desktop.session_patrol.source_projection import project_authoring_sources, freeze_authoring_sources, source_fingerprint
from focus.history import FocusItem


def test_stable_rows_select_one_expanded_call_without_touching_source():
    records = [{"role": "assistant", "content": "读取", "tool_calls": [
        {"id": "one", "name": "read", "args": {}}, {"id": "two", "name": "read", "args": {"path": "x"}}]},
        {"role": "user", "content": "相同"}, {"role": "user", "content": "相同"}]
    original = deepcopy(records)
    source = {"kind": "context", "revision_id": "R1"}
    rows = project_authoring_sources(records, source)
    again = project_authoring_sources(records, source)
    assert [row["source_row_id"] for row in rows] == [row["source_row_id"] for row in again]
    assert len({row["source_row_id"] for row in rows}) == len(rows)
    chosen = next(row for row in rows if row["entry"]["payload"].get("call_id") == "two")
    entries, frozen = freeze_authoring_sources(records, source, selected_rows=[chosen["source_row_id"], chosen["source_row_id"]])
    assert len(entries) == 1 and entries[0].kind == "function_call"
    assert entries[0].payload["call_id"] == "two"
    assert frozen[entries[0].source_ref]["record"] == original[0]
    assert records == original
    with pytest.raises(ValueError):
        freeze_authoring_sources(records, source, selected_rows=[])


def test_history_policy_is_shared_by_preview_and_import():
    records = [FocusItem(item_id="runtime", kind="world_state_update", origin="runtime", scope="runtime",
                        payload={"content": "旧事实"}).model_dump(mode="json"),
               FocusItem(item_id="opaque", kind="reasoning", origin="runtime", scope="runtime",
                         payload={"opaque": "私有证明"}).model_dump(mode="json")]
    source = {"kind": "context", "revision_id": "R1"}
    default = project_authoring_sources(records, source)
    historical = project_authoring_sources(records, source, include_historical=True)
    assert not any(row["eligible"] for row in default)
    assert [row["eligible"] for row in historical] == [True, False]
    assert [row["source_row_id"] for row in default] == [row["source_row_id"] for row in historical]
    entries, _ = freeze_authoring_sources(records, source, include_historical=True)
    assert len(entries) == 1 and entries[0].reference_only
    assert "旧事实" in entries[0].payload["content"]
    assert source_fingerprint(records, source) != source_fingerprint(records, source, include_historical=True)


@pytest.mark.usefixtures("isolated_postgres_database")
def test_preview_is_read_only_and_import_is_atomic_and_full(client):
    http, task, path, model_requests = client
    draft = save(http, open_draft(http, task), {"entries": [
        {"entry_id": "anchor", "kind": "message", "payload": {"role": "user", "content": "保留"}, "future": {"x": 1}}]}).json()
    source = {"kind": "file", "context_id": task["task_id"], "path": "source.txt"}
    text = "完整上下文" * 400
    path.joinpath("source.txt").write_text(text, encoding="utf-8")
    endpoint = f"/desktop/api/drafts/{draft['draft_id']}"
    preview = http.post(endpoint + "/source-preview", headers=SESSION, json=source)
    assert preview.status_code == 200, preview.text
    page = preview.json()
    row = page["rows"][0]
    assert row["truncated"] and len(row["summary"]) == 600
    full = http.post(endpoint + "/source-preview", headers=SESSION, json={**source, "row_id": row["source_row_id"]}).json()
    assert full["row"]["entry"]["payload"]["content"] == text
    current = http.get(endpoint, headers=SESSION).json()
    assert current["draft_revision"] == draft["draft_revision"] and current["authoring_document"] == draft["authoring_document"]
    request = {**source, "draft_revision": draft["draft_revision"], "selected_row_ids": [row["source_row_id"]],
               "source_fingerprint": page["source_fingerprint"], "before_entry_id": "anchor"}
    invalid = http.post(endpoint + "/sources", headers=SESSION, json={**request, "before_entry_id": "missing"})
    assert invalid.status_code == 409 and invalid.json()["detail"]["code"] == "anchor_missing"
    empty = http.post(endpoint + "/sources", headers=SESSION, json={**request, "selected_row_ids": []})
    assert empty.status_code == 422
    unknown = http.post(endpoint + "/sources", headers=SESSION, json={**request, "selected_row_ids": ["unavailable-row"]})
    assert unknown.status_code == 422 and unknown.json()["detail"]["code"] == "invalid_source_selection"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: http.post(endpoint + "/sources", headers=SESSION, json=request), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    imported = next(response.json() for response in responses if response.status_code == 200)
    entries = imported["authoring_document"]["entries"]
    assert len(entries) == 2 and entries[0]["payload"]["content"] == text
    assert entries[1] == draft["authoring_document"]["entries"][0]
    assert imported["draft_revision"] == draft["draft_revision"] + 1
    assert imported["inserted_entry_ids"] == [entries[0]["entry_id"]]
    assert imported["inserted_sources"][entries[0]["source_ref"]]["record"]["content"] == text
    assert not model_requests


@pytest.mark.usefixtures("isolated_postgres_database")
def test_source_changed_and_unavailable_are_recoverable(client):
    http, task, path, _ = client
    draft = open_draft(http, task)
    endpoint = f"/desktop/api/drafts/{draft['draft_id']}"
    source = {"kind": "file", "context_id": task["task_id"], "path": "changing.txt"}
    path.joinpath("changing.txt").write_text("版本一", encoding="utf-8")
    page = http.post(endpoint + "/source-preview", headers=SESSION, json=source).json()
    path.joinpath("changing.txt").write_text("版本二", encoding="utf-8")
    request = {**source, "draft_revision": draft["draft_revision"], "selected_row_ids": [page["rows"][0]["source_row_id"]],
               "source_fingerprint": page["source_fingerprint"], "before_entry_id": None}
    conflict = http.post(endpoint + "/sources", headers=SESSION, json=request)
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "source_changed"
    assert http.get(endpoint, headers=SESSION).json()["authoring_document"] == draft["authoring_document"]
    missing = http.post(endpoint + "/source-preview", headers=SESSION, json={"kind": "context", "revision_id": "missing"})
    assert missing.status_code == 404
    forbidden = http.post(endpoint + "/source-preview", headers=SESSION, json={**source, "path": "../outside.txt"})
    assert forbidden.status_code == 422


@pytest.mark.usefixtures("isolated_postgres_database")
def test_directory_query_and_cursor_are_scoped(client):
    from datetime import UTC, datetime
    import uuid
    from langchain_core.messages import HumanMessage
    from backend.app.gateway.app import app
    from backend.app.desktop.context_evolution import ContextRevisionRepository, ContextRevisionContract, ContextRevisionRef
    from focus.history import serialize_history_message, content_hash
    http, task, _, _ = client
    async def seed():
        service = app.state.desktop_service
        graph = await service.contexts._make_state_graph()
        for generation in (20, 21):
            config = await graph.aupdate_state({"configurable": {"thread_id": task["thread_id"], "checkpoint_ns": ""}},
                {"messages": [HumanMessage(content=f"来源 {generation}", id=f"catalog-{generation}")]}, as_node="model")
            state = await graph.aget_state(config)
            records = tuple(serialize_history_message(message) for message in state.values["messages"])
            ref = ContextRevisionRef(context_id=task["task_id"], revision_id=uuid.uuid4().hex, generation=generation,
                execution_thread_id=task["thread_id"], checkpoint_ns="", checkpoint_id=config["configurable"]["checkpoint_id"], payload_mode="checkpoint")
            async with service.session_factory() as session:
                await ContextRevisionRepository().insert(session, ContextRevisionContract(ref=ref, execution_messages=records,
                    content_hash=content_hash(records), projection_status="valid", origin_kind="root", created_at=datetime.now(UTC)))
                await session.commit()
    http.portal.call(seed)
    first = http.get("/desktop/api/patrol/sources?kind=context&limit=1", headers=SESSION)
    assert first.status_code == 200 and len(first.json()["items"]) == 1
    assert first.json()["next_cursor"]
    second = http.get("/desktop/api/patrol/sources", headers=SESSION, params={"kind": "context", "limit": 1, "cursor": first.json()["next_cursor"]})
    assert second.status_code == 200 and second.json()["items"][0]["source_ref"] != first.json()["items"][0]["source_ref"]
    legacy = http.get("/desktop/api/patrol/sources", headers=SESSION)
    assert legacy.status_code == 200 and "contexts" in legacy.json() and "branches" in legacy.json()
    if first.json()["next_cursor"]:
        mismatch = http.get("/desktop/api/patrol/sources", headers=SESSION, params={"kind": "patrol", "limit": 1, "cursor": first.json()["next_cursor"]})
        assert mismatch.status_code == 422


@pytest.mark.usefixtures("isolated_postgres_database")
def test_preview_paging_selection_and_query_cursor_share_one_snapshot(client, monkeypatch):
    from backend.app.gateway.app import app
    http, task, _, _ = client
    draft = open_draft(http, task)
    endpoint = f"/desktop/api/drafts/{draft['draft_id']}"
    records = [{"role": "user", "content": f"条目 {index}"} for index in range(5)]
    source = {"kind": "context", "context_id": task["task_id"], "revision_id": "frozen"}
    async def read(_session, _request):
        return deepcopy(records), source
    monkeypatch.setattr(app.state.desktop_service.patrol_sources._reader, "read", read)
    first = http.post(endpoint + "/source-preview", headers=SESSION, json={**source, "limit": 2}).json()
    second = http.post(endpoint + "/source-preview", headers=SESSION, json={**source, "limit": 2, "cursor": first["next_cursor"]}).json()
    ids = [first["rows"][0]["source_row_id"], second["rows"][0]["source_row_id"]]
    mismatch = http.post(endpoint + "/source-preview", headers=SESSION, json={**source, "cursor": first["next_cursor"], "query": "不同查询"})
    assert mismatch.status_code == 422
    filtered = http.post(endpoint + "/source-preview", headers=SESSION, json={**source, "query": "条目 2"}).json()
    assert filtered["rows"][0]["source_row_id"] == ids[1]
    imported = http.post(endpoint + "/sources", headers=SESSION, json={**source, "selected_row_ids": list(reversed(ids)),
        "source_fingerprint": first["source_fingerprint"], "before_entry_id": None, "draft_revision": draft["draft_revision"]})
    assert imported.status_code == 200, imported.text
    assert [entry["payload"]["content"] for entry in imported.json()["inserted_entries"]] == ["条目 0", "条目 2"]


def test_diagnostics_locate_actual_fields_and_offer_applicable_actions():
    from backend.app.desktop.session_patrol.contracts import AuthoringDocument
    from backend.app.desktop.session_patrol.compiler import compile_document
    document = AuthoringDocument.model_validate({"entries": [
        {"entry_id": "bad", "kind": "function_call", "payload": {"name": "read", "call_id": "bad", "arguments": "{bad"}},
        {"entry_id": "waiting", "kind": "function_call", "payload": {"name": "read", "call_id": "waiting", "arguments": "{}"}},
        {"entry_id": "orphan", "kind": "function_call_output", "payload": {"call_id": "missing", "output": "示例"}},
        {"entry_id": "output", "kind": "function_call_output", "payload": {"call_id": "waiting", "output": []}, "content_error": "未完成 JSON"}]})
    diagnostics = compile_document(document, provider="openai").diagnostics
    invalid = next(d for d in diagnostics if d["code"] == "invalid_tool_fields" and d["entry_ids"] == ["bad"])
    assert invalid["field"] == "payload.arguments" and "placeholder" not in invalid["options"]
    orphan = next(d for d in diagnostics if d["code"] == "orphan_tool_output")
    assert orphan["field"] == "payload.call_id" and "placeholder" not in orphan["options"]
    parsing = next(d for d in diagnostics if d["code"] == "entry_parse_error")
    assert parsing["field"] == "payload.output" and parsing["options"] == ["edit"]
    document.entries = [document.entries[1]]
    missing = next(d for d in compile_document(document, provider="openai").diagnostics if d["code"] == "missing_tool_output")
    assert "placeholder" in missing["options"]


@pytest.mark.usefixtures("isolated_postgres_database")
def test_provider_projection_failure_is_global_without_fake_entry_identity(client, monkeypatch):
    from focus.context.preparation import AgentRequestPreparation
    http, task, _, requests = client
    draft = save(http, open_draft(http, task), {"entries": [
        {"entry_id": "valid", "kind": "message", "payload": {"role": "user", "content": "保留原文"}}]}).json()
    def reject(_self, _messages, _context):
        raise ValueError("当前模型投影无法准备")
    monkeypatch.setattr(AgentRequestPreparation, "preview", reject)
    plan = http.post(f"/desktop/api/drafts/{draft['draft_id']}/preview", headers=SESSION).json()
    diagnostic = next(d for d in plan["diagnostics"] if d["code"] == "provider_projection")
    assert not plan["executable"] and diagnostic["entry_ids"] == [] and diagnostic["options"] == ["edit"]
    assert not requests


@pytest.mark.usefixtures("isolated_postgres_database")
def test_import_commit_failure_rolls_back_entries_references_and_revision(client, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    from backend.app.gateway.app import app
    from backend.app.desktop.models import PatrolDraft
    http, task, path, _ = client
    draft = open_draft(http, task)
    endpoint = f"/desktop/api/drafts/{draft['draft_id']}"
    path.joinpath("rollback.txt").write_text("事务失败不能留下半份来源", encoding="utf-8")
    source = {"kind": "file", "context_id": task["task_id"], "path": "rollback.txt"}
    page = http.post(endpoint + "/source-preview", headers=SESSION, json=source).json()
    async def reject_commit(_session):
        raise RuntimeError("commit interrupted")
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", reject_commit)
        with pytest.raises(RuntimeError, match="commit interrupted"):
            http.post(endpoint + "/sources", headers=SESSION, json={**source, "draft_revision": draft["draft_revision"],
                "selected_row_ids": [page["rows"][0]["source_row_id"]], "source_fingerprint": page["source_fingerprint"], "before_entry_id": None})
    current = http.get(endpoint, headers=SESSION).json()
    assert current["draft_revision"] == draft["draft_revision"] and current["authoring_document"] == draft["authoring_document"]
    async def audit():
        async with app.state.desktop_service.session_factory() as session:
            stored = await session.get(PatrolDraft, draft["draft_id"])
            assert stored.frozen_sources == {}
    http.portal.call(audit)
