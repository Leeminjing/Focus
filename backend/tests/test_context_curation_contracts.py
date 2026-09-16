r"""本文件验证命名空间多来源 evidence 与 create/update/keep/pause/retire Lane plan 合同。

输入为两个含相同 message id 的 Context revision、合法与恶意计划 payload；输出为无歧义 round-trip、
闭合 action 联合及裸 ID、未知 action、越界 evidence 拒绝断言。具体工作流为通过严格 Pydantic
discriminator 解析每种操作，并使用完整 Context/revision/checkpoint/message 键查找证据。
示例：`pytest backend/tests/test_context_curation_contracts.py`。
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from backend.app.desktop.context_curation import (
    LanePlan,
    MultiSourceEvidence,
    NamespacedMessageRef,
    PortfolioLanePlan,
    SourceMessageEvidence,
    SourceRevisionEvidence,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionPayloadMode,
    ContextRevisionRef,
)


def _ref(context_id: str, *, runnable: bool = True) -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id=context_id,
        revision_id=uuid.uuid4().hex,
        generation=1,
        execution_thread_id=f"thread-{context_id}",
        checkpoint_ns="",
        checkpoint_id=f"checkpoint-{context_id}" if runnable else None,
        payload_mode=ContextRevisionPayloadMode.DEFINITION,
    )


def _message_ref(source: ContextRevisionRef, message_id: str = "shared-message") -> dict:
    return {"source": source.model_dump(mode="json"), "message_id": message_id}


def test_identical_message_ids_remain_namespaced_through_round_trip() -> None:
    source_a = _ref("context-a")
    source_b = _ref("context-b")
    evidence = MultiSourceEvidence.model_validate(
        {
            "sources": [
                {
                    "source": source_a.model_dump(mode="json"),
                    "projection_hash": "a" * 64,
                    "content_hash": "b" * 64,
                    "messages": [
                        {
                            "ref": _message_ref(source_a),
                            "role": "human",
                            "content": "A requirement",
                        }
                    ],
                },
                {
                    "source": source_b.model_dump(mode="json"),
                    "projection_hash": "c" * 64,
                    "content_hash": "d" * 64,
                    "messages": [
                        {
                            "ref": _message_ref(source_b),
                            "role": "human",
                            "content": "B requirement",
                        }
                    ],
                },
            ]
        }
    )
    restored = MultiSourceEvidence.model_validate(evidence.model_dump(mode="json"))
    ref_a = NamespacedMessageRef.model_validate(_message_ref(source_a))
    ref_b = NamespacedMessageRef.model_validate(_message_ref(source_b))
    assert ref_a.key != ref_b.key
    assert restored.message(ref_a).content == "A requirement"
    assert restored.message(ref_b).content == "B requirement"

    plan = LanePlan.model_validate(
        {
            "action": "update",
            "lane_id": "testing-lane",
            "purpose": "Integrate requirements",
            "base_context_revision": source_a.model_dump(mode="json"),
            "publisher_epoch": 4,
            "source_frontier": [
                source_a.model_dump(mode="json"),
                source_b.model_dump(mode="json"),
            ],
            "items": [
                {
                    "type": "compose_message",
                    "role": "human",
                    "content": "Honor both requirements.",
                    "sources": [_message_ref(source_a), _message_ref(source_b)],
                }
            ],
        }
    )
    serialized = plan.model_dump(mode="json")
    assert LanePlan.model_validate(serialized) == plan
    assert serialized["items"][0]["sources"][0]["source"]["context_id"] == "context-a"
    assert serialized["items"][0]["sources"][1]["source"]["context_id"] == "context-b"


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        (
            "create",
            {
                "lane_id": None,
                "purpose": "New investigation",
                "source_frontier": [],
                "items": [],
            },
        ),
        (
            "keep",
            {
                "lane_id": "lane-1",
                "purpose": "Testing",
                "publisher_epoch": 1,
                "source_frontier": [],
                "semantic_fingerprint": "f" * 64,
            },
        ),
        (
            "pause",
            {"lane_id": "lane-1", "publisher_epoch": 1, "reason": "Wait"},
        ),
        (
            "retire",
            {"lane_id": "lane-1", "publisher_epoch": 1, "reason": "No longer useful"},
        ),
    ],
)
def test_closed_lane_actions_validate_required_fields(action: str, extra: dict) -> None:
    source = _ref("source")
    payload = {
        "action": action,
        "base_context_revision": source.model_dump(mode="json"),
        **extra,
    }
    if action == "create":
        payload["source_frontier"] = [source.model_dump(mode="json")]
        payload["items"] = [
            {"type": "copy_message", "source": _message_ref(source, "message-1")}
        ]
        payload.pop("base_context_revision")
    elif action == "keep":
        payload["source_frontier"] = [source.model_dump(mode="json")]
    assert LanePlan.model_validate(payload).action == action


def test_contract_rejects_bare_unknown_and_out_of_frontier_references() -> None:
    source = _ref("source")
    other = _ref("other")
    base = {
        "action": "create",
        "lane_id": None,
        "purpose": "Testing",
        "source_frontier": [source.model_dump(mode="json")],
    }
    with pytest.raises(ValidationError):
        LanePlan.model_validate(
            {**base, "items": [{"type": "copy_message", "source_message_id": "m1"}]}
        )
    with pytest.raises(ValidationError):
        LanePlan.model_validate({**base, "action": "publish", "items": []})
    with pytest.raises(ValidationError):
        LanePlan.model_validate(
            {
                **base,
                "items": [
                    {"type": "copy_message", "source": _message_ref(other, "m1")}
                ],
            }
        )
    with pytest.raises(ValidationError):
        NamespacedMessageRef(
            source=_ref("not-runnable", runnable=False),
            message_id="m1",
        )


def test_portfolio_plan_rejects_two_operations_for_one_lane() -> None:
    source = _ref("source")
    common = {
        "lane_id": "lane-1",
        "base_context_revision": source.model_dump(mode="json"),
        "publisher_epoch": 2,
    }
    with pytest.raises(ValidationError):
        PortfolioLanePlan.model_validate(
            {
                "lanes": [
                    {"action": "pause", "reason": "wait", **common},
                    {"action": "retire", "reason": "done", **common},
                ]
            }
        )
