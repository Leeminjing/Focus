r"""本文件验证确定性多来源 Lane compiler 的基数、消息协议、外部 lineage 与 evidence 边界。

输入为共享 message id 的两个来源、不同 Lane 计划及完整/缺失工具交换；输出为一对一、一对多、
多对一、多对多投影、稳定消息身份、逐来源处置和非法候选拒绝断言。具体工作流为只让计划选择
内容，所有 target ID、tool-call ID、execution projection 与 lineage 由 compiler 生成。
示例：`pytest backend/tests/test_context_curation_compiler.py`。
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.desktop.context_curation import (
    LaneCompilationError,
    MultiSourceEvidence,
    compile_lane,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionPayloadMode,
    ContextRevisionRef,
)


def _ref(context_id: str) -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id=context_id,
        revision_id=uuid.uuid5(uuid.NAMESPACE_DNS, context_id).hex,
        generation=1,
        execution_thread_id=f"thread-{context_id}",
        checkpoint_ns="",
        checkpoint_id=f"checkpoint-{context_id}",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )


def _message_ref(source: ContextRevisionRef, message_id: str) -> dict:
    return {"source": source.model_dump(mode="json"), "message_id": message_id}


def _evidence(
    source_a: ContextRevisionRef,
    source_b: ContextRevisionRef,
) -> MultiSourceEvidence:
    return MultiSourceEvidence.model_validate(
        {
            "sources": [
                {
                    "source": source_a.model_dump(mode="json"),
                    "projection_hash": "a" * 64,
                    "content_hash": "b" * 64,
                    "messages": [
                        {
                            "ref": _message_ref(source_a, "shared"),
                            "role": "human",
                            "content": "Implement the API.",
                        },
                        {
                            "ref": _message_ref(source_a, "status"),
                            "role": "ai",
                            "content": "Database layer is complete.",
                        },
                    ],
                },
                {
                    "source": source_b.model_dump(mode="json"),
                    "projection_hash": "c" * 64,
                    "content_hash": "d" * 64,
                    "messages": [
                        {
                            "ref": _message_ref(source_b, "shared"),
                            "role": "human",
                            "content": "Do not expand scope.",
                        },
                        {
                            "ref": _message_ref(source_b, "test-result"),
                            "role": "ai",
                            "content": "Three database tests fail.",
                        },
                    ],
                },
            ]
        }
    )


def _create_plan(
    purpose: str,
    frontier: list[ContextRevisionRef],
    items: list[dict],
) -> dict:
    return {
        "action": "create",
        "lane_id": None,
        "purpose": purpose,
        "source_frontier": [source.model_dump(mode="json") for source in frontier],
        "items": items,
    }


def test_compiler_supports_one_to_one_one_to_many_many_to_one_and_many_to_many() -> None:
    source_a = _ref("context-a")
    source_b = _ref("context-b")
    evidence = _evidence(source_a, source_b)
    implementation = _create_plan(
        "Implementation",
        [source_a],
        [{"type": "copy_message", "source": _message_ref(source_a, "shared")}],
    )
    architecture = _create_plan(
        "Architecture",
        [source_a],
        [
            {
                "type": "compose_message",
                "role": "human",
                "content": "Review the architecture before expanding implementation.",
                "sources": [
                    _message_ref(source_a, "shared"),
                    _message_ref(source_a, "status"),
                ],
            }
        ],
    )
    testing = _create_plan(
        "Testing",
        [source_a, source_b],
        [
            {
                "type": "compose_message",
                "role": "human",
                "content": "Diagnose the three failures without expanding scope.",
                "sources": [
                    _message_ref(source_a, "status"),
                    _message_ref(source_b, "shared"),
                    _message_ref(source_b, "test-result"),
                ],
            }
        ],
    )
    requirement = _create_plan(
        "Requirement",
        [source_b],
        [{"type": "copy_message", "source": _message_ref(source_b, "shared")}],
    )

    compiled = [
        compile_lane(plan, evidence)
        for plan in (implementation, architecture, testing, requirement)
    ]
    assert all(candidate.projection_status == "valid" for candidate in compiled)
    assert compiled[0] == compile_lane(implementation, evidence)
    assert compiled[0].authored_messages[0]["id"] != compiled[1].authored_messages[0]["id"]
    assert "source" not in compiled[2].authored_messages[0]
    assert "curation" not in compiled[2].authored_messages[0]
    testing_sources = compiled[2].message_lineage[0].sources
    assert {source.source.context_id for source in testing_sources} == {
        "context-a",
        "context-b",
    }
    assert {item.source.key for item in compiled[2].source_dispositions} == {
        message.ref.key
        for bundle in evidence.sources
        for message in bundle.messages
    }
    assert sum(
        item.action == "used" for item in compiled[2].source_dispositions
    ) == 3


def test_compiler_rejects_unknown_evidence_and_broken_tool_protocol() -> None:
    source = _ref("tool-context")
    other = _ref("other-context")
    evidence = MultiSourceEvidence.model_validate(
        {
            "sources": [
                {
                    "source": source.model_dump(mode="json"),
                    "projection_hash": "a" * 64,
                    "content_hash": "b" * 64,
                    "messages": [
                        {
                            "ref": _message_ref(source, "caller"),
                            "role": "ai",
                            "content": "",
                            "tool_calls": [
                                {"id": "call-1", "name": "read_file", "args": {"path": "a.py"}}
                            ],
                        }
                    ],
                }
            ]
        }
    )
    unknown = _create_plan(
        "Unknown",
        [source],
        [{"type": "copy_message", "source": _message_ref(source, "missing")}],
    )
    broken_copy = _create_plan(
        "Broken copy",
        [source],
        [{"type": "copy_message", "source": _message_ref(source, "caller")}],
    )
    incomplete_exchange = _create_plan(
        "Broken exchange",
        [source],
        [
            {
                "type": "tool_exchange",
                "assistant_content": "",
                "calls": [
                    {
                        "name": "read_file",
                        "args": {"path": "a.py"},
                        "result_content": "contents",
                    }
                ],
                "sources": [_message_ref(source, "caller")],
            }
        ],
    )
    missing_bundle = _create_plan(
        "Missing bundle",
        [other],
        [{"type": "copy_message", "source": _message_ref(other, "m1")}],
    )
    for plan in (unknown, broken_copy, incomplete_exchange, missing_bundle):
        with pytest.raises(LaneCompilationError):
            compile_lane(plan, evidence)


def test_compiler_rebuilds_complete_tool_exchange_with_system_owned_ids() -> None:
    source = _ref("tool-context")
    evidence = MultiSourceEvidence.model_validate(
        {
            "sources": [
                {
                    "source": source.model_dump(mode="json"),
                    "projection_hash": "a" * 64,
                    "content_hash": "b" * 64,
                    "messages": [
                        {
                            "ref": _message_ref(source, "caller"),
                            "role": "ai",
                            "content": "Reading file",
                            "tool_calls": [
                                {"id": "call-1", "name": "read_file", "args": {"path": "a.py"}}
                            ],
                        },
                        {
                            "ref": _message_ref(source, "result"),
                            "role": "tool",
                            "content": "contents",
                            "tool_call_id": "call-1",
                            "name": "read_file",
                            "status": "success",
                        },
                    ],
                }
            ]
        }
    )
    plan = _create_plan(
        "Evidence",
        [source],
        [
            {
                "type": "tool_exchange",
                "assistant_content": "Reading file",
                "calls": [
                    {
                        "name": "read_file",
                        "args": {"path": "a.py"},
                        "result_content": "contents",
                        "status": "success",
                    }
                ],
                "sources": [
                    _message_ref(source, "caller"),
                    _message_ref(source, "result"),
                ],
            }
        ],
    )
    compiled = compile_lane(plan, evidence)
    assert [message["role"] for message in compiled.execution_messages] == ["ai", "tool"]
    call_id = compiled.execution_messages[0]["tool_calls"][0]["id"]
    assert call_id.startswith("focus-curation-call-")
    assert compiled.execution_messages[1]["tool_call_id"] == call_id
    assert all(len(item.sources) == 2 for item in compiled.message_lineage)
