r"""本文件对外提供 LoopLiveProjectionReducer 与 ProjectionSequenceGap。

输入为不可变 LoopLiveProjection 和下一条 CanonicalEventEnvelope；输出为确定性新投影。具体工作流为先执行
sequence 去重/缺口检查，再按 entity_type 调用单实体或实体集合 reducer（含 Context 派生边集合），拒绝陈旧
同一 entity identity 的 revision；切换实体时重新建立状态，以 Round 单调轮号及 Patrol 的当前 Round 绑定拒绝迟到旧实体，历史 tool fact 不进入领域集合，最后追加有界安全活动摘要；未知 kind 保持前向兼容但不修改已知实体。
示例：`next_state = reducer.reduce(state, event)`。
用户 intervention 按其不可变 revision 更新独立集合，活动保留 origin/type/state 供真实因果展示。
"""

from __future__ import annotations

from typing import ClassVar

from backend.app.desktop.agent_loop.event_contract import CanonicalEventEnvelope
from backend.app.desktop.agent_loop.live_projection_contract import (
    ActivityEntry,
    LoopLiveProjection,
    ProjectedEntity,
)


class ProjectionSequenceGap(RuntimeError):
    pass


class LoopLiveProjectionReducer:
    _SINGULAR = frozenset({"loop", "mission", "patrol_session", "round", "portfolio", "accounting"})
    _ACTIVITY_ONLY = frozenset({"tool", "artifact", "model_call", "loop_activity"})
    _COLLECTIONS: ClassVar[dict[str, str]] = {
        "context": "contexts",
        "intervention": "interventions",
        "context_lineage": "lineage",
        "run": "runs",
        "context_run": "runs",
        "curator": "curators",
        "context_expansion": "expansions",
        "directive": "directives",
        "fact": "facts",
        "loop_wait_request": "wait_requests",
        "loop_wait_response": "wait_responses",
    }

    def __init__(self, timeline_limit: int = 200) -> None:
        self._timeline_limit = max(1, timeline_limit)

    def reduce(self, projection: LoopLiveProjection, event: CanonicalEventEnvelope) -> LoopLiveProjection:
        if event.loop_id != projection.loop_id:
            raise ValueError("event 不属于当前 Loop projection")
        if event.sequence <= projection.last_sequence:
            return projection
        if event.sequence != projection.last_sequence + 1:
            raise ProjectionSequenceGap(f"expected={projection.last_sequence + 1}, actual={event.sequence}")
        changes = self._entity_change(projection, event)
        timeline = (*projection.activity_timeline, self._activity(event))[-self._timeline_limit :]
        unknown = projection.unknown_kinds
        if event.entity_type not in self._SINGULAR and event.entity_type not in self._COLLECTIONS and event.entity_type not in self._ACTIVITY_ONLY:
            unknown = tuple(dict.fromkeys((*unknown, event.kind)))
        return projection.model_copy(update={**changes, "last_sequence": event.sequence, "activity_timeline": timeline, "unknown_kinds": unknown})

    def _entity_change(self, projection: LoopLiveProjection, event: CanonicalEventEnvelope) -> dict:
        if event.entity_type == "fact" and (event.payload.get("fact_type") or event.payload.get("kind")) == "tool":
            return {"facts": {key: value for key, value in projection.facts.items() if key != event.entity_id}}
        payload = self._normalized_payload(event.entity_type, event.payload)
        if event.correlation_id is not None and "correlation_id" not in payload:
            payload = {**payload, "correlation_id": event.correlation_id}
        if event.entity_type in self._SINGULAR:
            current = getattr(projection, event.entity_type)
            if event.entity_type == "patrol_session" and projection.round is not None and payload.get("round_id") not in {None, projection.round.entity_id}:
                return {}
            same = current is not None and current.entity_id == event.entity_id
            if same and current.revision >= event.entity_revision:
                return {}
            if event.entity_type == "round" and current is not None and not same and int(payload.get("number") or 0) <= int(current.state.get("number") or 0):
                return {}
            state = {**(current.state if same else {}), **payload}
            entity = ProjectedEntity(entity_id=event.entity_id, revision=event.entity_revision, updated_sequence=event.sequence, state=state)
            changes = {event.entity_type: entity}
            if event.entity_type == "round" and not same and projection.patrol_session is not None:
                if projection.patrol_session.state.get("round_id") != event.entity_id:
                    changes["patrol_session"] = None
            return changes
        field = self._COLLECTIONS.get(event.entity_type)
        if field is None:
            if event.entity_type in self._ACTIVITY_ONLY:
                run_id = event.payload.get("run_id")
                prior = projection.runs.get(run_id) if run_id else None
                if prior is not None:
                    updated = prior.model_copy(update={
                        "updated_sequence": event.sequence,
                        "state": {
                            **prior.state,
                            "last_activity_at": event.occurred_at.isoformat(),
                            "last_activity_kind": event.kind,
                            "last_activity_summary": event.payload.get("summary") or event.kind,
                        },
                    })
                    return {"runs": {**projection.runs, run_id: updated}}
            return {}
        current = getattr(projection, field)
        prior = current.get(event.entity_id)
        state = {**(prior.state if prior is not None else {}), **payload}
        entity = ProjectedEntity(entity_id=event.entity_id, revision=event.entity_revision, updated_sequence=event.sequence, state=state)
        if prior is not None and prior.revision >= entity.revision:
            return {}
        return {field: {**current, event.entity_id: entity}}

    @staticmethod
    def _normalized_payload(entity_type: str, payload: dict) -> dict:
        if entity_type == "fact" and "fact_type" in payload:
            presentation = payload.get("presentation") or {}
            return {
                **payload,
                "kind": payload.get("fact_type"),
                "status": payload.get("state"),
                "context_id": payload.get("source_context_id"),
                "run_id": payload.get("source_run_id"),
                "title": presentation.get("title"),
                "summary": presentation.get("summary"),
                "metrics": presentation.get("metrics") or {},
                "outcome_status": presentation.get("outcome_status"),
            }
        if entity_type == "patrol_session":
            return {**payload, "safe_summary": payload.get("safe_summary") or payload.get("summary")}
        if entity_type == "curator":
            return {**payload, "safe_summary": payload.get("safe_summary") or payload.get("result_summary") or payload.get("summary")}
        if entity_type == "context_expansion":
            return {**payload, "safe_summary": payload.get("safe_summary") or payload.get("summary") or payload.get("state")}
        return payload

    @staticmethod
    def _activity(event: CanonicalEventEnvelope) -> ActivityEntry:
        summary = event.payload.get("summary") or event.payload.get("status") or event.kind
        detail = {key: event.payload.get(key) for key in ("context_id", "source_context_id", "run_id", "tool_name", "status", "directive_id", "opportunity_id", "blocker_code", "origin", "intent_kind", "state") if event.payload.get(key) is not None}
        return ActivityEntry(event_id=event.event_id, sequence=event.sequence, kind=event.kind, entity_type=event.entity_type, entity_id=event.entity_id, summary=str(summary)[:500], occurred_at=event.occurred_at, correlation_id=event.correlation_id, causation_id=event.causation_id, detail=detail)
