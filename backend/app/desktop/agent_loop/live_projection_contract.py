r"""本文件对外提供 LoopLiveProjection、ProjectedEntity、ActivityEntry 与 ProjectionDiagnostics。

输入为规范事件归并后的 Loop、Patrol、round、wait、Context/Run、curator、expansion、directive、fact、portfolio 与
Context 派生边状态；输出为可序列化的单边界 Live Snapshot。具体工作流为每个实体保留 revision/sequence，
timeline 有界保存安全摘要，last_sequence 标记整个快照的提交边界。示例：`LoopLiveProjection(loop_id="l1")`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectedEntity(_ProjectionModel):
    entity_id: str
    revision: int = Field(ge=1)
    updated_sequence: int = Field(ge=1)
    state: dict[str, Any] = Field(default_factory=dict)


class ActivityEntry(_ProjectionModel):
    event_id: str
    sequence: int
    kind: str
    entity_type: str
    entity_id: str
    summary: str
    occurred_at: datetime
    correlation_id: str | None = None
    causation_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ProjectionDiagnostics(_ProjectionModel):
    journal_last_sequence: int = 0
    projector_last_sequence: int = 0
    lag: int = 0
    rebuilt: bool = False
    recovery_status: str = "healthy"
    degraded_scope: tuple[str, ...] = ()
    quarantined_units: tuple[str, ...] = ()
    updated_at: datetime | None = None


class LoopLiveProjection(_ProjectionModel):
    loop_id: str
    last_sequence: int = 0
    loop: ProjectedEntity | None = None
    mission: ProjectedEntity | None = None
    patrol_session: ProjectedEntity | None = None
    round: ProjectedEntity | None = None
    contexts: dict[str, ProjectedEntity] = Field(default_factory=dict)
    lineage: dict[str, ProjectedEntity] = Field(default_factory=dict)
    runs: dict[str, ProjectedEntity] = Field(default_factory=dict)
    curators: dict[str, ProjectedEntity] = Field(default_factory=dict)
    expansions: dict[str, ProjectedEntity] = Field(default_factory=dict)
    directives: dict[str, ProjectedEntity] = Field(default_factory=dict)
    facts: dict[str, ProjectedEntity] = Field(default_factory=dict)
    wait_requests: dict[str, ProjectedEntity] = Field(default_factory=dict)
    wait_responses: dict[str, ProjectedEntity] = Field(default_factory=dict)
    portfolio: ProjectedEntity | None = None
    activity_timeline: tuple[ActivityEntry, ...] = ()
    unknown_kinds: tuple[str, ...] = ()
    diagnostics: ProjectionDiagnostics = Field(default_factory=ProjectionDiagnostics)
