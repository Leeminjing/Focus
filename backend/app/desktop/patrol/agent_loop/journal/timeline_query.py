"""Sequence-based history and incremental reads, independent of journal lifetime."""
from fastapi import HTTPException
from sqlalchemy import or_, select

from backend.app.desktop.patrol.agent_loop.journal.event_contract import LiveEventAuthorizationPolicy
from backend.app.desktop.patrol.agent_loop.journal.journal_models import LoopJournalSequence, LoopProjectorCursor
from backend.app.desktop.patrol.agent_loop.live.live_access import LoopLiveAccessPolicy, LoopLiveRedactionPolicy
from backend.app.desktop.patrol.agent_loop.facts.materialized_fact_query import MaterializedFactQueryService
from backend.app.desktop.patrol.agent_loop.journal.timeline_models import WorkTimelineEntry
from backend.app.desktop.patrol.agent_loop.journal.timeline_projector import WorkTimelineProjector


class WorkTimelineQuery:
    async def read(self, session, loop_id, *, before=None, after=None, limit=80,
                   category=None, context_id=None, round_id=None, intent_id=None):
        if before is not None and after is not None:
            raise HTTPException(422, "before 和 after 不能同时使用")
        if category not in {None, "fact", "user_decision"}:
            raise HTTPException(422, "不支持的时间线筛选")
        await MaterializedFactQueryService._authorize_scope(session, loop_id, context_id)
        access = await LoopLiveAccessPolicy().resolve(session, loop_id)
        cursor = await session.get(LoopProjectorCursor, (loop_id, WorkTimelineProjector.PROJECTOR_NAME))
        projected = cursor.last_sequence if cursor else 0
        sequence = await session.get(LoopJournalSequence, loop_id)
        e = WorkTimelineEntry
        predicates = [e.loop_id == loop_id, e.sequence <= projected]
        predicates.append(or_(e.visibility["required_permissions"].is_(None),
                              e.visibility["required_permissions"].contained_by(list(access.permissions))))
        if category:
            predicates.append(e.category == category)
        if context_id:
            predicates.append(e.context_ids.contains([context_id]))
        if round_id:
            predicates.append(e.round_id == round_id)
        if intent_id:
            decisions = select(e.decision_id).where(
                e.loop_id == loop_id, e.category == "user_decision",
                e.references["intent_ids"].contains([intent_id]), e.decision_id.is_not(None),
            )
            predicates.append(or_(e.intent_id == intent_id, e.references["intent_ids"].contains([intent_id]), e.decision_id.in_(decisions)))
        if before is not None:
            predicates.append(e.sequence < before)
        if after is not None:
            predicates.append(e.sequence > after)
        limit = max(1, min(limit, 200))
        rows = list(await session.scalars(select(e).where(*predicates).order_by(
            e.sequence.asc() if after is not None else e.sequence.desc(),
        ).limit(limit + 1)))
        has_more = len(rows) > limit
        rows = rows[:limit]
        if after is None:
            rows.reverse()
        return {
            "loop_id": loop_id, "entries": [self.serialize(row, access.permissions) for row in rows],
            "has_more": has_more,
            "next_before": rows[0].sequence if rows and after is None and has_more else None,
            # Advance over non-display events too, but never skip a remaining page.
            "next_after": rows[-1].sequence if rows and after is not None and has_more else max(after or 0, projected),
            "projected_through": projected, "source_sequence": sequence.last_sequence if sequence else 0,
        }

    @staticmethod
    def serialize(row, permissions):
        refs = LiveEventAuthorizationPolicy._redact_value(row.references)
        return LoopLiveRedactionPolicy.redact_value({
            "entry_id": row.entry_id, "source_event_id": row.source_event_id, "sequence": row.sequence,
            "kind": row.kind, "category": row.category, "title": row.title, "summary": row.summary,
            "round_id": row.round_id, "round_number": row.round_number, "context_ids": row.context_ids,
            "run_id": row.run_id, "fact_id": row.fact_id, "fact_revision": row.fact_revision,
            "intent_id": row.intent_id, "decision_id": row.decision_id,
            "correlation_id": row.correlation_id, "causation_id": row.causation_id,
            "references": refs,
            "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
            "recorded_at": row.recorded_at.isoformat(),
        }, permissions)
