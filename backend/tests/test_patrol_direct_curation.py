r"""本文件验证 Portfolio Patrol 对简单 Lane 变化拥有不经 Worker 的直接策展路径。

输入为单 Lane create 判断、精确来源 evidence 与 Patrol rationale；输出为完整 valid candidate、
外部 lineage 及空 Worker request/attempt。具体工作流为 PatrolCurationEngine 直接编译最终计划，
并在显式直达预算超限时拒绝本轮而不暗中创建 Worker。示例：
`pytest backend/tests/test_patrol_direct_curation.py`。
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.desktop.context_curation import (
    DirectCurationBudgetExceeded,
    PatrolCurationEngine,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionPayloadMode,
    ContextRevisionRef,
)


def _source() -> ContextRevisionRef:
    return ContextRevisionRef(
        context_id="main-context",
        revision_id=uuid.uuid4().hex,
        generation=3,
        execution_thread_id="thread-main",
        checkpoint_ns="",
        checkpoint_id="checkpoint-main-3",
        payload_mode=ContextRevisionPayloadMode.CHECKPOINT,
    )


def _request(source: ContextRevisionRef, lane_count: int = 1) -> dict:
    source_payload = source.model_dump(mode="json")
    message_ref = {"source": source_payload, "message_id": "goal-message"}
    return {
        "program_id": "program-1",
        "portfolio_revision_id": "portfolio-4",
        "observed_frontier_hash": "f" * 64,
        "rationale": "Database work is complete; direct the next Lane to testing.",
        "plan": {
            "lanes": [
                {
                    "action": "create",
                    "lane_id": None,
                    "purpose": f"Testing {index}",
                    "source_frontier": [source_payload],
                    "items": [
                        {
                            "type": "compose_message",
                            "role": "human",
                            "content": "Run database tests and fix only relevant failures.",
                            "sources": [message_ref],
                        }
                    ],
                }
                for index in range(lane_count)
            ]
        },
        "evidence": {
            "sources": [
                {
                    "source": source_payload,
                    "projection_hash": "a" * 64,
                    "content_hash": "b" * 64,
                    "messages": [
                        {
                            "ref": message_ref,
                            "role": "human",
                            "content": "Complete the feature and test it.",
                        }
                    ],
                }
            ]
        },
    }


def test_patrol_directly_curates_one_lane_with_zero_worker_attempts() -> None:
    decision = PatrolCurationEngine().decide_direct(_request(_source()))
    assert len(decision.lanes) == 1
    assert decision.lanes[0].candidate is not None
    assert decision.lanes[0].candidate.projection_status == "valid"
    assert decision.lanes[0].candidate.authored_messages[0]["role"] == "human"
    assert decision.worker_request_ids == ()
    assert decision.worker_attempt_ids == ()


def test_direct_budget_failure_does_not_implicitly_create_workers() -> None:
    engine = PatrolCurationEngine(max_direct_lane_mutations=1)
    with pytest.raises(DirectCurationBudgetExceeded):
        engine.decide_direct(_request(_source(), lane_count=2))
