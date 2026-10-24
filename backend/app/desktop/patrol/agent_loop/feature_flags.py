r"""本文件对外提供 LoopFeatureFlags 与布尔环境值解析。

输入为可选环境映射；输出为 canonical event、supervisor、materialized fact、Live API、frontend projection 与自动 Context 写入开关。
具体工作流为逐项读取显式环境变量，未配置时保持新架构默认启用，非法值立即失败；示例：`LoopFeatureFlags.from_env()`。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LoopFeatureFlags:
    canonical_event_emission: bool = True
    supervisor_scheduling: bool = True
    materialized_fact_reads: bool = True
    live_api: bool = True
    frontend_projection: bool = True
    automatic_context_expansion_writes: bool = True

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> LoopFeatureFlags:
        source = os.environ if environment is None else environment
        return cls(
            canonical_event_emission=_boolean(source, "FOCUS_LOOP_CANONICAL_EVENTS", True),
            supervisor_scheduling=_boolean(source, "FOCUS_LOOP_SUPERVISOR", True),
            materialized_fact_reads=_boolean(source, "FOCUS_LOOP_MATERIALIZED_FACTS", True),
            live_api=_boolean(source, "FOCUS_LOOP_LIVE_API", True),
            frontend_projection=_boolean(source, "FOCUS_LOOP_FRONTEND_PROJECTION", True),
            automatic_context_expansion_writes=_boolean(
                source,
                "FOCUS_LOOP_AUTOMATIC_CONTEXT_EXPANSION_WRITES",
                True,
            ),
        )

    def public_payload(self) -> dict[str, bool]:
        return {"liveLoopProjection": self.frontend_projection}


def _boolean(source: Mapping[str, str], key: str, default: bool) -> bool:
    value = source.get(key)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} 必须是布尔值")
