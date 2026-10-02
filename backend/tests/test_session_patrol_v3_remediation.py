"""本文件提供会话 Patrol v3 核验修复的来源与双 Provider 请求回归。

输入为隔离的 opaque/可读历史以及用户编写协作内容；输出为正文/来源保真与实际请求语义断言。
工作流使用生产来源投影、编译器、typed bridge 和 request projector，拒绝把私有连续性或证明包装成正文。
示例：pytest backend/tests/test_session_patrol_v3_remediation.py -q。
"""
from copy import deepcopy
import json

import pytest
from backend.app.desktop.session_patrol.compiler import compile_document
from backend.app.desktop.session_patrol.contracts import AuthoringDocument
from backend.app.desktop.session_patrol.source_projection import freeze_authoring_sources
from focus.history import FocusItem, content_hash, items_to_messages, messages_to_items
from focus.models.provider_contract import ProviderContract
from focus.models.response_projection import ResponsesRequestProjector


@pytest.mark.parametrize("historical", [False, True])
def test_opaque_items_never_become_authored_references(historical):
    records = [FocusItem(item_id=kind, kind=kind, origin="runtime" if kind == "projection_repair" else "provider",
                         payload={"type": kind, "encrypted_content": "FAKE_OPAQUE", "proof": {"private": True}}).model_dump(mode="json")
               for kind in ["reasoning", "compaction", "unknown", "projection_repair"]]
    before = deepcopy(records)
    entries, sources = freeze_authoring_sources(records, {"revision_id": "r"}, include_historical=historical)
    assert entries == [] and sources == {}
    assert records == before and content_hash(records) == content_hash(before)


def test_historical_reference_exposes_only_readable_content():
    record = FocusItem(item_id="permission", kind="world_state_update", origin="runtime", scope="runtime", payload={"message": {
        "role": "system", "content": [{"type": "text", "text": "旧权限：read-only"},
                                        {"type": "reasoning", "encrypted_content": "PRIVATE"}],
        "additional_kwargs": {"proof": "PRIVATE"}}}).model_dump(mode="json")
    before = deepcopy(record)
    entries, sources = freeze_authoring_sources([record], {"revision_id": "r"}, include_historical=True)
    assert len(entries) == 1 and entries[0].kind == "selected_context" and entries[0].reference_only
    assert "旧权限：read-only" in entries[0].payload["content"]
    assert "PRIVATE" not in json.dumps(entries[0].model_dump())
    assert record == before and sources[entries[0].source_ref]["record"] == before
    compiled = compile_document(AuthoringDocument(entries=entries), provider="openai", sources=sources)
    assert compiled.executable and compiled.items[0]["origin"] == "user_authored"


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize("content", ["same finding", [{"type": "input_text", "text": "same finding"},
                                                       {"type": "input_image", "image_url": "https://example.com/fake.png"}]])
def test_collaboration_preserves_participants_and_content_in_request(provider, content):
    document = AuthoringDocument.model_validate({"entries": [
        {"entry_id": "first", "kind": "agent_collaboration", "payload": {"author": "teammate-A", "recipient": "patrol-B", "content": content}},
        {"entry_id": "second", "kind": "agent_collaboration", "payload": {"author": "teammate-C", "recipient": "patrol-D", "content": content}}]})
    before = document.model_dump()
    compiled = compile_document(document, provider=provider)
    assert compiled.executable, compiled.diagnostics
    items = tuple(FocusItem.model_validate(value) for value in compiled.items)
    messages = items_to_messages(items)
    assert messages_to_items(messages) == items
    request = ResponsesRequestProjector(ProviderContract(provider, "responses", supports_image_input=True)).build(messages, model="fake-model")
    for index, (author, recipient) in enumerate([("teammate-A", "patrol-B"), ("teammate-C", "patrol-D")]):
        wire = request["input"][index]
        assert wire["type"] == "message" and wire["role"] == "user"
        rendered = json.dumps(wire, ensure_ascii=False)
        assert author in rendered and recipient in rendered and "same finding" in rendered
        assert "不代表真实通信或执行证明" in rendered
        if isinstance(content, list):
            assert wire["content"][-1] == content[-1]
    assert request["input"][0] != request["input"][1]
    assert document.model_dump() == before
