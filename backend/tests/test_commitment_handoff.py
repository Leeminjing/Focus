"""本文件对外提供合同生产、批准证明、冻结引用和双 Provider 投影的无网络合同测试。

输入为宿主 stage7 批准／stage9 checkpoint 与明文 Inbox fixtures；输出为来源、稳定身份、语义资格和冲突拒绝断言。
具体工作流为编译独立合同与参考、模拟篡改／重复交付，再核对 typed bridge、RSI／Curator 和 display／wire 的各自投影。
示例：pytest backend/tests/test_commitment_handoff.py；不执行真实 Provider 或文件数据库迁移。
"""

from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend.app.desktop.context_curator.projector import CurationSourceProjector
from backend.app.desktop.context_evolution.history import continued_revision_history
from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import RevisionSemanticIndexer
from backend.app.desktop.agent_loop.context_expansion.semantic_index import RevisionSemanticIndex
from backend.app.desktop.agent_loop.context_expansion.contracts import SemanticEvidenceUnit
from backend.app.desktop.context_curation import NamespacedMessageRef
from backend.tests.test_context_revision_reader import _ref
from backend.app.desktop.context_evolution import ContextRevisionPayloadMode
from focus.agents.commitment.artifacts import _freeze_knowledge
from focus.agents.commitment.handoff import ContractApproval, compile_handoff, validate_contract_mirror
from focus.history import content_hash, messages_to_items, semantic_messages, serialize_history_message
from focus.history.bridge import replace_execution_items, synchronize_items
from focus.models.provider_contract import ProviderContract
from focus.models.response_projection import ResponsesRequestProjector
from focus.runtime.runs.events import serialize_value


def _completed():
    trigger = HumanMessage(id="input", content="/commit 做X", additional_kwargs={"focus_context": {
        "kind": "message", "origin": "direct_user", "scope": "revision"}})
    artifact = {"contract_markdown": "# Confirmed task\n\nImplement X."}
    snapshots = _freeze_knowledge({"knowledge": [{"technology": "LangGraph", "version": "1.2.6",
                                               "source_url": "https://docs.langchain.com", "content": "FROZEN_KNOWLEDGE"}]})
    state = {"stage": 9, "artifacts": {"7": artifact}, "task_contract": artifact["contract_markdown"],
             "messages": [trigger], "knowledge_snapshots": snapshots,
             "knowledge_files": [snapshot["path"] for snapshot in snapshots],
             "contract_approval": ContractApproval(artifact_ref="requirements/task/task-contract.md",
                contract_hash=content_hash(artifact["contract_markdown"]), artifact_hash=content_hash(artifact)).model_dump()}
    checkpoint = {"thread_id": "task:commitment", "checkpoint_ns": "", "checkpoint_id": "exact-child"}
    return state, trigger, checkpoint


def test_compiler_produces_separate_typed_contract_and_references():
    state, trigger, checkpoint = _completed()
    messages = compile_handoff(state, trigger, checkpoint, [{"kind": "run", "run_id": "run"}])
    items = messages_to_items(messages)
    assert [item.kind for item in items] == ["task_contract", "selected_context"]
    assert all(item.origin == "delegated" and item.scope == "revision" for item in items)
    assert all(item.item_id != trigger.id for item in items)
    assert messages[0].content == state["task_contract"] and "FROZEN_KNOWLEDGE" not in messages[0].content
    assert any(ref.get("checkpoint_id") == "exact-child" for ref in items[0].source_refs)
    assert any(ref.get("contract_hash") == content_hash(messages[0].content) for ref in items[0].source_refs)
    assert messages == compile_handoff(state, trigger, checkpoint, [{"kind": "run", "run_id": "run"}])
    validate_contract_mirror([trigger, *messages], state["task_contract"])
    with pytest.raises(ValueError, match="canonical"):
        validate_contract_mirror(messages, "conflicting mirror")


@pytest.mark.parametrize("alteration", ["draft", "awaiting", "missing_approval", "approval_status", "contract", "artifact", "knowledge", "missing_knowledge", "source", "checkpoint"])
def test_compiler_rejects_unapproved_or_conflicting_sources(alteration):
    state, trigger, checkpoint = _completed()
    if alteration == "draft":
        state["stage"] = 7
    elif alteration == "awaiting":
        state["awaiting_human"] = 7
    elif alteration == "missing_approval":
        state.pop("contract_approval")
    elif alteration == "approval_status":
        state["contract_approval"]["status"] = "draft"
    elif alteration == "contract":
        state["task_contract"] += " changed after approval"
    elif alteration == "artifact":
        state["artifacts"]["7"]["extra"] = "changed artifact"
    elif alteration == "knowledge":
        state["knowledge_snapshots"][0]["content"] = "changed knowledge"
    elif alteration == "missing_knowledge":
        state["knowledge_snapshots"] = []
    elif alteration == "source":
        state["messages"] = [HumanMessage(id="different-input", content="x")]
    else:
        checkpoint.pop("checkpoint_id")
    with pytest.raises(ValueError):
        compile_handoff(state, trigger, checkpoint, [])


def test_new_approved_version_gets_new_identity_and_old_items_cannot_change():
    state, trigger, checkpoint = _completed()
    original = compile_handoff(state, trigger, checkpoint, [])
    updated = deepcopy(state)
    updated["task_contract"] += "\nNew approved version."
    updated["artifacts"]["7"]["contract_markdown"] = updated["task_contract"]
    updated["contract_approval"].update(contract_hash=content_hash(updated["task_contract"]),
                                       artifact_hash=content_hash(updated["artifacts"]["7"]))
    replacement = compile_handoff(updated, trigger, {**checkpoint, "checkpoint_id": "new-approved"}, [])
    assert original[0].id != replacement[0].id
    records = synchronize_items(None, [trigger, *original])
    assert replace_execution_items(records, records) == records
    changed = original[0].model_copy(update={"content": "illegal replacement"})
    with pytest.raises(ValueError, match="不可原位"):
        replace_execution_items(records, synchronize_items(None, [trigger, changed, original[1]]))


def test_contract_authored_semantic_curator_and_display_keep_separate_authorities():
    state, trigger, checkpoint = _completed()
    delivery = compile_handoff(state, trigger, checkpoint, [])
    messages = [trigger, *delivery, AIMessage(id="answer", content="unverified outcome")]
    records = [serialize_history_message(message) for message in messages]
    payload = continued_revision_history([records[0]], records)
    assert [item.kind for item in payload.authored_items] == ["message", "task_contract", "selected_context"]
    semantic = semantic_messages(payload.execution_items)
    assert [row["semantic_policy"] for row in semantic] == ["index", "index", "reference_only", "index"]
    curator = CurationSourceProjector().project("parent-checkpoint", list(semantic))
    assert [row.semantic_policy for row in curator.messages] == ["index", "index", "reference_only", "index"]
    source = _ref("contract", "contract-r", ContextRevisionPayloadMode.CHECKPOINT)
    index = RevisionSemanticIndexer().index(source=source, source_content_hash=content_hash(records),
        context_role="main", active_objective="task", raw_messages=semantic)
    assert all("FROZEN_KNOWLEDGE" not in unit.statement for unit in index.semantic_units)
    assert payload.execution_items[0].payload["message"]["content"] == trigger.content
    display = serialize_value({"messages": messages})["messages"]
    assert [row["id"] for row in display] == [trigger.id, "answer"]
    assert display[0]["content"].count("FROZEN_KNOWLEDGE") == 1
    assert messages[0].content == "/commit 做X" and delivery[0].id != trigger.id
    legacy = messages_to_items([HumanMessage(id="old", content="<task_contract>draft</task_contract>")])
    assert legacy[0].kind == "message" and legacy[0].origin == "legacy_unknown"


def test_v7_task_fallback_changes_fingerprint_without_rewriting_legacy_index():
    state, trigger, checkpoint = _completed()
    messages = [trigger, *compile_handoff(state, trigger, checkpoint, [])]
    source = _ref("index", "index-r", ContextRevisionPayloadMode.CHECKPOINT)
    current = RevisionSemanticIndexer().index(source=source, source_content_hash=content_hash("same-source"),
        context_role="main", active_objective="task", raw_messages=semantic_messages(messages_to_items(messages)))
    legacy_unit = SemanticEvidenceUnit.create(kind="claim", authority="hypothesis", statement=current.segments[0].descriptor,
        evidence_refs=tuple(NamespacedMessageRef(source=source, message_id=message.message_id) for message in current.messages))
    legacy = RevisionSemanticIndex.create(source=source, source_content_hash=current.source_content_hash,
        context_role=current.context_role, active_objective=current.active_objective, messages=current.messages,
        segments=current.segments, semantic_units=(legacy_unit,), fallback_segment_ids=current.fallback_segment_ids,
        quality_state="degraded", index_schema_version="revision-semantic-index-v6",
        segmenter_version=current.segmenter_version, projector_version=current.projector_version)
    stored = deepcopy(legacy.model_dump(mode="json"))
    assert "FROZEN_KNOWLEDGE" in legacy.semantic_units[0].statement
    assert all("FROZEN_KNOWLEDGE" not in unit.statement for unit in current.semantic_units)
    assert current.index_schema_version == RevisionSemanticIndexer.INDEX_SCHEMA_VERSION and current.index_id != legacy.index_id
    assert RevisionSemanticIndex.model_validate(stored) == legacy
    assert legacy.model_dump(mode="json") == stored


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_host_collaboration_and_contract_use_user_wire_without_beta(provider):
    state, trigger, checkpoint = _completed()
    inbox = HumanMessage(id="inbox:1", content="Teammate evidence", additional_kwargs={"focus_context": {
        "kind": "agent_collaboration", "origin": "collaborator", "scope": "execution",
        "source_refs": [{"kind": "inbox", "message_id": "1", "author": "teammate", "recipient": "main"}]}})
    messages = [inbox, *compile_handoff(state, trigger, checkpoint, [])]
    request = ResponsesRequestProjector(ProviderContract(provider, "responses")).build(messages, model="fixture")
    assert all(item["type"] == "message" and item["role"] == "user" for item in request["input"])
    assert "multi_agent" not in request and "betas" not in request
    assert [item.origin for item in messages_to_items(messages)] == ["collaborator", "delegated", "delegated"]
    assert semantic_messages(messages_to_items(messages))[0]["semantic_policy"] == "evidence_only"
    native = AIMessage(id="native", content="", additional_kwargs={"focus_response_items": [
        {"type": "agent_message", "id": "beta", "content": [{"type": "encrypted_content", "encrypted_content": "opaque"}]}]},
        response_metadata={"provider": provider, "projection_version": "focus-responses-v1"})
    assert messages_to_items([native])[0].kind == "unknown"
    with pytest.raises(ValueError):
        ResponsesRequestProjector(ProviderContract(provider, "responses")).build([native], model="fixture")
