r"""本文件对外提供 LoopLiveProjection、ProjectedEntity、ActivityEntry 与 ProjectionDiagnostics。

输入为规范事件归并后的 Loop、Patrol、round、wait、Context/Run、curator、expansion、directive、fact、portfolio 与
Context 派生边和独立 accounting 状态；输出为可序列化的单边界 Live Snapshot。Round、Observation、Session 按身份保存，round/patrol_session 仅为当前集合成员的兼容投影，不能独立写入。具体工作流为每个实体保留 revision/sequence，消费读模型不改变 Loop 控制版本，
timeline 有界保存安全摘要，last_sequence 标记整个快照的提交边界。示例：`LoopLiveProjection(loop_id="l1")`。
interventions 独立集合保存类型化用户消息交付状态，和 Run 当前状态分离。
task_progress 是独立单实体，identity 为 Loop，revision 为已提交 generation+1；未完成沉淀不替换 head。
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
    rounds: dict[str, ProjectedEntity] = Field(default_factory=dict)
    observations: dict[str, ProjectedEntity] = Field(default_factory=dict)
    patrol_sessions: dict[str, ProjectedEntity] = Field(default_factory=dict)
    accounting: ProjectedEntity | None = None
    task_progress: ProjectedEntity | None = None
    contexts: dict[str, ProjectedEntity] = Field(default_factory=dict)
    interventions: dict[str, ProjectedEntity] = Field(default_factory=dict)
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


def current_entities(projection: LoopLiveProjection) -> LoopLiveProjection:
    rounds = projection.rounds or ({projection.round.entity_id: projection.round} if projection.round else {})
    sessions = projection.patrol_sessions or ({projection.patrol_session.entity_id: projection.patrol_session} if projection.patrol_session else {})
    current = max(rounds.values(), key=lambda item: int(item.state.get("number") or 0), default=None)
    observation_id = current.state.get("observation_id") if current else None
    eligible = [item for item in sessions.values() if current is not None
                and item.state.get("round_id") == current.entity_id
                and (not observation_id or item.state.get("observation_id") == observation_id)]
    patrol = max(eligible, key=lambda item: item.updated_sequence, default=None)
    return projection.model_copy(update={"rounds": rounds, "patrol_sessions": sessions,
                                         "round": current, "patrol_session": patrol})
