r"""本文件验证 Live Loop 五个发布阶段可以独立切换。

输入为逐项环境映射、禁用的 canonical journal 与 Live API policy；输出为每个开关互不影响、非法值失败、禁用事件不访问数据库的断言。
具体工作流为纯配置解析后对中央 journal gate 做一次异步调用；示例：`pytest backend/tests/test_loop_feature_flags.py`。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.feature_flags import LoopFeatureFlags


@pytest.mark.parametrize(
    ("environment_key", "field"),
    (
        ("FOCUS_LOOP_CANONICAL_EVENTS", "canonical_event_emission"),
        ("FOCUS_LOOP_SUPERVISOR", "supervisor_scheduling"),
        ("FOCUS_LOOP_MATERIALIZED_FACTS", "materialized_fact_reads"),
        ("FOCUS_LOOP_LIVE_API", "live_api"),
        ("FOCUS_LOOP_FRONTEND_PROJECTION", "frontend_projection"),
    ),
)
def test_each_loop_stage_can_be_disabled_independently(environment_key: str, field: str) -> None:
    flags = LoopFeatureFlags.from_env({environment_key: "false"})
    values = {
        "canonical_event_emission": flags.canonical_event_emission,
        "supervisor_scheduling": flags.supervisor_scheduling,
        "materialized_fact_reads": flags.materialized_fact_reads,
        "live_api": flags.live_api,
        "frontend_projection": flags.frontend_projection,
    }
    assert values[field] is False
    assert all(value is True for key, value in values.items() if key != field)


def test_invalid_loop_flag_fails_during_startup() -> None:
    with pytest.raises(ValueError, match="必须是布尔值"):
        LoopFeatureFlags.from_env({"FOCUS_LOOP_LIVE_API": "sometimes"})


def test_disabled_canonical_emission_does_not_touch_the_session() -> None:
    class Session:
        def __getattr__(self, name):
            raise AssertionError(f"disabled journal accessed session.{name}")

    async def run() -> None:
        result = await LoopEventJournal(enabled=False).append(
            Session(),
            "loop-1",
            CanonicalEventDraft(kind="loop.test", entity_type="loop", entity_id="loop-1", entity_revision=1, idempotency_key="test:1"),
        )
        assert result is None

    asyncio.run(run())
