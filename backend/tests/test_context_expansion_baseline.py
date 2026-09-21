r"""本文件对外提供 Context expansion 前后关键行为的回归测试。

输入为单 Context observation、简单 continue action 与语义派生 action；输出为 Bootstrap Curator scope、continue 与
semantic spawn 合同断言。具体工作流为只构造不可变合同并调用现有公开 interface，
不连接数据库或产生权威写入。示例：`pytest backend/tests/test_context_expansion_baseline.py`。
"""

from __future__ import annotations

from backend.app.desktop.agent_loop.patrol_runtime import CuratorCoordinationStage
import pytest
from pydantic import ValidationError

from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope, PATROL_ACTION_ADAPTER, PATROL_MODEL_ACTION_ADAPTER


def _single_context_observation() -> LoopObservationEnvelope:
    return LoopObservationEnvelope(
        loop_id="loop-baseline",
        loop_revision=1,
        round_id="round-baseline",
        goal_revision=1,
        authority_revision=1,
        observed_frontier_hash="a" * 64,
        mission={"outcome": "Implement and independently verify the change"},
        grant={},
        portfolio_frontier=(
            {
                "lane_id": "lane-primary",
                "context_id": "context-primary",
                "revision_id": "revision-primary",
                "role": "primary",
            },
        ),
        workspace={"revision": 1},
        budget={},
    )


def test_single_context_receives_bootstrap_curator_scope() -> None:
    stage = CuratorCoordinationStage(object())

    assert stage.scopes(_single_context_observation()) == (
        {
            "mode": "bootstrap",
            "lane_id": "lane-primary",
            "context_id": "context-primary",
            "revision_id": "revision-primary",
            "role": "primary",
        },
    )


def test_multi_context_curator_scope_is_lane_bounded() -> None:
    observation = _single_context_observation().model_copy(
        update={
            "portfolio_frontier": tuple(
                {
                    "lane_id": f"lane-{index}",
                    "context_id": f"context-{index}",
                    "revision_id": f"revision-{index}",
                    "role": f"role-{index}",
                }
                for index in range(6)
            )
        }
    )
    scopes = CuratorCoordinationStage(object(), max_assignments=3).scopes(observation)

    assert len(scopes) == 3
    assert {scope["mode"] for scope in scopes} == {"lane"}


def test_continue_context_currently_uses_a_small_semantic_action() -> None:
    action = PATROL_ACTION_ADAPTER.validate_python(
        {
            "action": "continue_context",
            "context_id": "context-primary",
            "context_revision_id": "revision-primary",
            "message": "Run the focused verification.",
        }
    )

    assert action.action == "continue_context"


def test_semantic_spawn_is_the_patrol_model_contract() -> None:
    action = PATROL_MODEL_ACTION_ADAPTER.validate_python(
        {
            "action": "spawn_context",
            "opportunity_id": "a" * 64,
            "source_context_id": "context-primary",
            "purpose": "Independent verification",
            "work_order": "Verify the implementation without modifying it.",
            "completion_check": "Report reproducible evidence.",
            "workspace_mode": "read_only",
        }
    )

    assert action.action == "spawn_context"
    with pytest.raises(ValidationError):
        PATROL_MODEL_ACTION_ADAPTER.validate_python(
            {"action": "create_lane", "plan": {}, "message": "Model must not construct this."}
        )
