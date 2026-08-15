"""验证 Context 执行投影只做无损补齐，任何降级都等待用户批准。"""

from copy import deepcopy

from backend.app.desktop.context_projection import compile_context_messages
from focus.runtime.runs.events import validate_messages


def test_valid_messages_are_unchanged():
    authored = [
        {"role": "human", "content": "查天气"},
        {
            "role": "ai",
            "content": "",
            "tool_calls": [{"id": "call-1", "name": "weather", "args": {"city": "北京"}}],
        },
        {"role": "tool", "content": "晴", "tool_call_id": "call-1", "name": "weather"},
    ]
    before = deepcopy(authored)

    result = compile_context_messages(authored)

    assert result.status == "valid"
    assert result.execution_messages == before
    assert result.authored_messages == before
    assert authored == before


def test_orphan_tool_message_gets_additive_call_placeholder():
    authored = [
        {"role": "tool", "content": "只保留的结果", "tool_call_id": "call-kept", "name": "search"}
    ]

    result = compile_context_messages(authored)

    assert result.status == "repaired"
    assert result.execution_messages[-1] == authored[0]
    assert result.execution_messages[0]["curation_synthetic"] is True
    assert result.execution_messages[0]["tool_calls"][0]["id"] == "call-kept"
    validate_messages(result.execution_messages)


def test_missing_tool_result_gets_additive_result_placeholder():
    authored = [
        {
            "role": "ai",
            "content": "",
            "tool_calls": [{"id": "call-1", "name": "read", "args": {}}],
        },
        {"role": "human", "content": "继续"},
    ]

    result = compile_context_messages(authored)

    assert result.status == "repaired"
    assert result.execution_messages[0] == authored[0]
    assert result.execution_messages[1]["role"] == "tool"
    assert result.execution_messages[2] == authored[1]
    validate_messages(result.execution_messages)


def test_invalid_message_requires_approval_without_silent_adoption():
    authored = [{"role": "tool", "content": "结果，没有协议字段"}]

    result = compile_context_messages(authored)

    assert result.status == "approval_required"
    assert result.authored_messages == authored
    assert result.issues[0]["original"] == authored[0]
    assert result.issues[0]["diff"]["operation"] == "replace"
    assert result.execution_messages[0]["role"] == "human"


def test_duplicate_call_id_and_invalid_content_require_approval():
    result = compile_context_messages(
        [
            {"role": "ai", "content": "", "tool_calls": [{"id": "same", "name": "a", "args": {}}]},
            {"role": "tool", "content": "ok", "tool_call_id": "same", "name": "a"},
            {"role": "ai", "content": "", "tool_calls": [{"id": "same", "name": "b", "args": {}}]},
            {"role": "human", "content": {"not": "a supported content value"}},
        ]
    )

    assert result.status == "approval_required"
    assert any("重复" in issue["reason"] for issue in result.issues)
    assert any("content" in issue["reason"] for issue in result.issues)


def test_regex_signature_never_scans_message_content():
    authored = [{"role": "human", "content": "A T tool_call_id=call-1 [HAS]T"}]

    result = compile_context_messages(authored)

    assert result.status == "valid"
    assert result.protocol_signature == "H"
    assert result.repair_manifest == []


def test_orphan_tool_after_unfinished_call_repairs_both_boundaries():
    authored = [
        {"role": "ai", "content": "", "tool_calls": [{"id": "first", "name": "read", "args": {}}]},
        {"role": "tool", "content": "second result", "tool_call_id": "second", "name": "search"},
    ]

    result = compile_context_messages(authored)

    assert result.status == "repaired"
    validate_messages(result.execution_messages)
    assert [repair["kind"] for repair in result.repair_manifest if repair["kind"] != "regex_flag"] == [
        "missing_tool_result",
        "missing_tool_call",
    ]
