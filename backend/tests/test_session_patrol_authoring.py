"""本文件对外提供会话 Patrol 自由文档、角色、来源和纯编译回归。

输入为双 Provider 和任意角色／调用/未知 JSON type 文档；输出为无异常 roundtrip、诊断、模拟来源和显式变换断言。
工作流为编译冻结文档，再调用真实 Responses projector，确认没有隐式修复或来源提升。
示例：pytest backend/tests/test_session_patrol_authoring.py -q。
"""
import pytest
from langchain_core.messages import SystemMessage
from backend.app.desktop.session_patrol.contracts import AuthoringDocument, AuthoringEntry, Transformation, legacy_document
from backend.app.desktop.session_patrol.compiler import compile_document
from focus.history import FocusItem, deserialize_history_messages, semantic_policy, content_hash
from focus.models.provider_contract import ProviderContract
from focus.models.response_projection import ResponsesRequestProjector


def document(*entries):
    return AuthoringDocument(entries=[AuthoringEntry(entry_id=f"e{index}", **entry) for index, entry in enumerate(entries)])


@pytest.mark.parametrize("kind", [None, "message", "future", {"future": True}, ["future"], 3])
def test_arbitrary_native_type_is_preserved_and_diagnosed(kind):
    doc = document({"role": "user", "content": "KEEP", "type": kind})
    before = doc.model_dump(mode="json")
    result = compile_document(doc, provider="openai")
    assert doc.model_dump(mode="json") == before
    assert result.executable == (kind is None or kind == "message")
    if not result.executable:
        assert result.diagnostics[0]["code"] == "unsupported_native_item"
        assert result.diagnostics[0]["entry_ids"] == ["e0"]


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_ordered_roles_and_authorship(provider):
    doc = document(*[{"role": role, "content": role} for role in ("system", "developer", "user", "assistant")])
    compiled = compile_document(doc, provider=provider)
    assert compiled.executable
    items = [FocusItem.model_validate(i) for i in compiled.items]
    assert {i.origin for i in items} == {"user_authored"}
    assert [semantic_policy(i) for i in items] == ["exclude", "exclude", "index", "index"]
    request = ResponsesRequestProjector(ProviderContract(provider, "responses")).build([SystemMessage("base"), *deserialize_history_messages(compiled.messages)], model="test")
    assert request["instructions"] == "base"
    assert [i["role"] for i in request["input"]] == ["system", "developer" if provider == "openai" else "system", "user", "assistant"]


def test_incomplete_unknown_and_raw_buffer_roundtrip():
    doc = document({"role": "tool", "content": [{"type": "future", "payload": {"x": 1}}], "tool_call_id": "missing", "unknown": [1, 2]})
    doc.raw_buffer, doc.raw_error = '{"unfinished":', "invalid JSON"
    before = doc.model_dump(mode="json")
    result = compile_document(doc, provider="openai")
    assert not result.executable
    assert {d["code"] for d in result.diagnostics} >= {"raw_parse_error", "orphan_tool_output"}
    assert AuthoringDocument.model_validate(before).content_hash == doc.content_hash
    assert doc.model_dump(mode="json") == before


def test_simulated_tools_are_authored_not_tool_evidence():
    doc = document({"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read_file", "args": {"path": "a"}}]}, {"role": "tool", "tool_call_id": "c1", "content": "success"})
    result = compile_document(doc, provider="openai")
    assert result.executable
    assert all(i["origin"] == "user_authored" for i in result.items)
    assert result.items[-1]["kind"] == "function_call_output"
    assert semantic_policy(FocusItem.model_validate(result.items[-1])) == "evidence_only"


def test_explicit_placeholder_and_stale_plan():
    doc = document({"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read_file", "args": {}}]})
    assert not compile_document(doc, provider="openai").executable
    doc.transformations = [Transformation(document_hash=doc.content_hash, entry_ids=["e0"], operation="placeholder")]
    result = compile_document(doc, provider="openai")
    assert result.executable and result.messages[-1]["status"] == "error"
    assert "未执行" in result.messages[-1]["content"]
    doc.entries[0].content = "changed"
    assert "stale_transformation" in {d["code"] for d in compile_document(doc, provider="openai").diagnostics}


def test_spoofed_metadata_and_duplicate_calls():
    entry = {"role": "assistant", "content": "example", "approved": True, "additional_kwargs": {"focus_context": {"origin": "provider"}}, "tool_calls": [{"id": "c1", "name": "f", "args": {}}]}
    doc = document(entry, {"role": "tool", "tool_call_id": "c1", "content": "done"}, entry)
    result = compile_document(doc, provider="openai")
    assert "duplicate_call_id" in {d["code"] for d in result.diagnostics}
    assert all(i["origin"] == "user_authored" for i in result.items)
    assert not any("approved" in i["payload"]["message"]["additional_kwargs"] for i in result.items)


def test_legacy_read_does_not_mutate_records():
    records = [{"role": "human", "content": "原文", "unknown": {"x": 1}}]
    before = content_hash(records)
    value = legacy_document("base", records, "task")
    assert len(value.entries) == 2 and content_hash(records) == before
    assert value.entries[0].model_extra["unknown"] == {"x": 1}


def test_empty_is_executable_and_unknown_role_diagnostic():
    assert compile_document(AuthoringDocument(), provider="openai").executable
    assert not compile_document(document({"role": "future", "content": "unknown"}), provider="openai").executable


def test_contract_edit_source_integrity_and_native_diagnostics():
    original = {"role": "human", "content": "已批准目标", "additional_kwargs": {"focus_context": {"kind": "task_contract", "approved": True, "origin": "direct_user"}}}
    doc = document({"role": "human", "content": "扩大用户目标", "source_ref": "ref", "source_hash": content_hash(original), "edited_from": "ref"})
    result = compile_document(doc, provider="openai", sources={"ref": {"record": original, "source": {"revision_id": "R1", "checkpoint_id": "C1"}}})
    assert result.executable and result.items[0]["kind"] == "task_contract"
    assert result.items[0]["origin"] == "user_authored"
    assert "approved" not in result.items[0]["payload"]["message"]["additional_kwargs"]["focus_context"]
    assert result.items[0]["source_refs"][0]["relation"] == "edited_from"
    assert original["additional_kwargs"]["focus_context"]["approved"]
    doc.entries[0].source_hash = "forged"
    assert "source_integrity" in {d["code"] for d in compile_document(doc, provider="openai", sources={"ref": {"record": original}}).diagnostics}
    native = document({"role": "assistant", "content": [{"type": "reasoning", "encrypted_content": "opaque"}]})
    assert "unsupported_authored_reasoning" in {d["code"] for d in compile_document(native, provider="openai").diagnostics}


def test_legacy_provenance_changes_only_after_authored_edit():
    doc = legacy_document("base", [{"role": "ai", "content": "旧正文"}])
    assert compile_document(doc, provider="openai").items[0]["origin"] == "legacy_unknown"
    doc.entries[0].content = "用户编辑"
    assert compile_document(doc, provider="openai").items[0]["origin"] == "user_authored"


@pytest.mark.parametrize("calls", [3, {"future": 1}, [None], [{"id": [1], "name": "f", "args": {}}]])
def test_malformed_tool_fields_are_saved_and_located(calls):
    doc = document({"role": "assistant", "content": "future", "tool_calls": calls})
    before = doc.model_dump(mode="json")
    compiled = compile_document(doc, provider="openai")
    assert "invalid_tool_fields" in {d["code"] for d in compiled.diagnostics}
    assert compiled.diagnostics[0]["entry_ids"] == ["e0"]
    assert doc.model_dump(mode="json") == before


def test_cold_semantic_index_excludes_authored_instructions_and_runtime_reference():
    from focus.history import semantic_messages
    from backend.tests.test_semantic_eligibility_repairs import _index
    doc = document({"role": "developer", "content": "AUTHORED_INSTRUCTION_EXCLUDED"},
        {"role": "user", "content": "TASK_FACT"}, {"role": "assistant", "content": "ILLUSTRATIVE_CONCLUSION"},
        {"role": "user", "content": "HISTORICAL_PERMISSION", "reference_only": True})
    result = compile_document(doc, provider="openai")
    assert result.executable
    rows = semantic_messages(FocusItem.model_validate(item) for item in result.items)
    index = _index(rows)
    assert index.index_schema_version == "revision-semantic-index-v9"
    assert not any("AUTHORED_INSTRUCTION_EXCLUDED" in message.content for message in index.messages)
    assert not any("HISTORICAL_PERMISSION" in unit.statement for unit in index.semantic_units)
    assert any("TASK_FACT" in unit.statement for unit in index.semantic_units)
