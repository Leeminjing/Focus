r"""本文件对外提供 LoopLiveProjectionReducer 与 ProjectionSequenceGap。

输入为不可变 LoopLiveProjection 和下一条 CanonicalEventEnvelope；输出为确定性新投影。具体工作流为先执行
sequence 去重/缺口检查，再按 entity_type 调用单实体或实体集合 reducer，拒绝陈旧 entity revision，最后追加
有界安全活动摘要；未知 kind 保持前向兼容但不修改已知实体。示例：`next_state = reducer.reduce(state, event)`。
"""

from __future__ import annotations

from backend.app.desktop.agent_loop.event_contract import CanonicalEventEnvelope
from backend.app.desktop.agent_loop.live_projection_contract import ActivityEntry, LoopLiveProjection, ProjectedEntity


class ProjectionSequenceGap(RuntimeError):
    pass


class LoopLiveProjectionReducer:
    _SINGULAR = frozenset({"loop", "mission", "patrol_session", "round", "portfolio"})
    _COLLECTIONS = {"context": "contexts", "run": "runs", "context_run": "runs", "curator": "curators", "directive": "directives", "fact": "facts"}

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
        if event.entity_type not in self._SINGULAR and event.entity_type not in self._COLLECTIONS:
            unknown = tuple(dict.fromkeys((*unknown, event.kind)))
        return projection.model_copy(update={**changes, "last_sequence": event.sequence, "activity_timeline": timeline, "unknown_kinds": unknown})

    def _entity_change(self, projection: LoopLiveProjection, event: CanonicalEventEnvelope) -> dict:
        payload = self._normalized_payload(event.entity_type, event.payload)
        if event.correlation_id is not None and "correlation_id" not in payload:
            payload = {**payload, "correlation_id": event.correlation_id}
        if event.entity_type in self._SINGULAR:
            current = getattr(projection, event.entity_type)
            state = {**(current.state if current is not None else {}), **payload}
            entity = ProjectedEntity(entity_id=event.entity_id, revision=event.entity_revision, updated_sequence=event.sequence, state=state)
            return {} if current is not None and current.revision >= entity.revision else {event.entity_type: entity}
        field = self._COLLECTIONS.get(event.entity_type)
        if field is None:
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
        return payload

    @staticmethod
    def _activity(event: CanonicalEventEnvelope) -> ActivityEntry:
        summary = event.payload.get("summary") or event.payload.get("status") or event.kind
        detail = {key: event.payload.get(key) for key in ("context_id", "run_id", "tool_name", "status", "directive_id") if event.payload.get(key) is not None}
        return ActivityEntry(event_id=event.event_id, sequence=event.sequence, kind=event.kind, entity_type=event.entity_type, entity_id=event.entity_id, summary=str(summary)[:500], occurred_at=event.occurred_at, correlation_id=event.correlation_id, causation_id=event.causation_id, detail=detail)
