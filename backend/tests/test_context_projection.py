r"""本文件对外提供 Context 消息协议投影的回归测试。

输入为合法、缺失、重复或中断的 AI/Tool 消息序列；输出为不可变 authored history、Provider 合法 execution view、
显式 repair manifest 与 approval-required 判定的断言。具体工作流为调用公开投影入口，验证每个 tool call 恰有一个匹配
结果且合成结果不冒充成功，后续 Run 不得认领早期悬空调用。示例：`pytest backend/tests/test_context_projection.py -q`。
"""

from copy import deepcopy

from backend.app.desktop.context_projection import ProtocolRepairContext, compile_context_messages
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

    assert result.status == "approval_required"
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

    assert result.status == "approval_required"
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

    assert result.status == "approval_required"
    validate_messages(result.execution_messages)
    assert [repair["kind"] for repair in result.repair_manifest if repair["kind"] != "regex_flag"] == [
        "missing_tool_result",
        "missing_tool_call",
    ]


def test_interrupted_tool_call_is_closed_with_explicit_non_success_result():
    authored = [
        {
            "role": "ai",
            "content": "",
            "tool_calls": [
                {
                    "id": "browser_run_code_unsafe",
                    "name": "browser_run_code_unsafe",
                    "args": {"code": "return await inspectPage()"},
                }
            ],
        }
    ]

    result = compile_context_messages(
        authored,
        ProtocolRepairContext.interrupted(
            "run-interrupted",
            call_ids=("browser_run_code_unsafe",),
            reason="The Run was interrupted while the browser tool was executing.",
            source_revision_id="revision-interrupted",
        ),
    )

    synthetic = result.execution_messages[-1]
    assert synthetic["role"] == "tool"
    assert synthetic["tool_call_id"] == "browser_run_code_unsafe"
    assert synthetic["name"] == "browser_run_code_unsafe"
    assert synthetic["status"] == "error"
    assert synthetic["focus_interruption_status"] == "interrupted"
    assert "interrupted" in synthetic["content"].casefold()
    assert result.authored_messages == authored
    assert result.status == "repaired"
    assert result.repair_manifest[0]["source_revision_id"] == "revision-interrupted"
    validate_messages(result.execution_messages)


def test_interrupted_multiple_calls_are_closed_once_and_recompile_idempotently():
    authored = [
        {
            "role": "ai",
            "content": "",
            "tool_calls": [
                {"id": "call-1", "name": "read", "args": {"path": "a"}},
                {"id": "call-2", "name": "read", "args": {"path": "b"}},
            ],
        }
    ]
    context = ProtocolRepairContext.interrupted(
        "run-multiple",
        call_ids=("call-1", "call-2"),
        status="cancelled",
        reason="The Run was cancelled.",
        source_revision_id="revision-multiple",
    )

    first = compile_context_messages(authored, context)
    second = compile_context_messages(authored, context)

    assert first.payload() == second.payload()
    results = [message for message in first.execution_messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in results] == ["call-1", "call-2"]
    assert all(message["status"] == "error" for message in results)
    assert all(message["focus_interruption_status"] == "cancelled" for message in results)
    assert all(item["source_run_id"] == "run-multiple" for item in first.repair_manifest if item["kind"] == "missing_tool_result")
    validate_messages(first.execution_messages)


def test_later_run_cannot_claim_an_earlier_unresolved_call() -> None:
    authored = [
        {
            "role": "ai",
            "id": "earlier-caller",
            "content": "",
            "tool_calls": [{"id": "earlier-call", "name": "write_file", "args": {}}],
        },
        {"role": "human", "id": "later-message", "content": "Continue after the earlier failure."},
    ]

    projection = compile_context_messages(
        authored,
        ProtocolRepairContext.interrupted(
            "later-run",
            call_ids=("earlier-call",),
            reason="provider 400: insufficient tool messages",
            source_revision_id="later-revision",
        ),
    )

    assert projection.status == "approval_required"
    assert projection.authored_messages == authored
    assert not any(
        repair.get("source_run_id") == "later-run" and repair.get("cause") == "interrupted"
        for repair in projection.repair_manifest
    )
