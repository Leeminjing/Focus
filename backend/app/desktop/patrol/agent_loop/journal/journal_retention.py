"""Compute a contiguous deletable journal prefix while preserving durable evidence.

The maintenance caller holds the parent Loop and sequence locks when applying a
plan. Unknown/uninitialised fact projectors retain the entire journal. Domain
facts, observations, revisions, files and checkpoints are never deleted here.
"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.orm import aliased

from backend.app.desktop.patrol.agent_loop.facts.fact_models import LoopFact, LoopFactRelationship, LoopFactRevision
from backend.app.desktop.patrol.agent_loop.journal.journal_models import LoopJournalEvent, LoopJournalSequence, LoopProjectorCursor
from backend.app.desktop.patrol.agent_loop.models import LoopObservation, LoopUserIntent
from backend.app.desktop.patrol.agent_loop.projections.projection_models import LoopProjectionFailure


async def cleanup_boundary(session, loop_id, retention_seconds, *, now=None, limit=5000):
    now = now or datetime.now(UTC)
    event = LoopJournalEvent
    sequence = await session.get(LoopJournalSequence, loop_id)
    if sequence is None:
        return 1
    boundary = sequence.last_sequence + 1
    # Facts and durable work history are mandatory before their first cursor;
    # all other registered consumers must also have consumed the prefix.
    cursors = dict((await session.execute(select(LoopProjectorCursor.projector_name, LoopProjectorCursor.last_sequence)
                                          .where(LoopProjectorCursor.loop_id == loop_id))).all())
    if not {"loop_facts", "work_timeline"}.issubset(cursors):
        return 1
    boundary = min(boundary, min(cursors.values()) + 1)
    retained = await session.scalar(select(func.min(event.sequence)).where(
        event.loop_id == loop_id,
        or_(event.occurred_at >= now - timedelta(seconds=retention_seconds), event.retained_until > now),
    ))
    if retained is not None:
        boundary = min(boundary, retained)
    failure = await session.scalar(select(func.min(LoopProjectionFailure.boundary_sequence)).where(
        LoopProjectionFailure.loop_id == loop_id, LoopProjectionFailure.unit_kind == "journal_event",
        LoopProjectionFailure.status != "resolved",
    ))
    if failure is not None:
        boundary = min(boundary, failure)
    # Bound the candidate set before checking nested provenance references.
    tail = await session.scalar(select(event.sequence).where(event.loop_id == loop_id)
                                .order_by(event.sequence).offset(limit).limit(1))
    if tail is not None:
        boundary = min(boundary, tail)
    # Protect typed provenance as well as nested evidence references. Conservative
    # text containment also recognises historical JSON reference shapes.
    references = []
    for model, fields in (
        (LoopFact, (LoopFact.evidence,)),
        (LoopFactRevision, (LoopFactRevision.cause_event_id, LoopFactRevision.evidence)),
        (LoopFactRelationship, (LoopFactRelationship.cause_event_id,)),
        (LoopObservation, (LoopObservation.envelope,)),
        (LoopUserIntent, (LoopUserIntent.request_payload,)),
    ):
        references.append(select(model.loop_id).where(model.loop_id == loop_id,
                          or_(*(cast(field, Text).contains(event.event_id, autoescape=False) for field in fields))).exists())
    child = aliased(event)
    references.append(select(child.event_id).where(child.loop_id == loop_id, child.causation_id == event.event_id).exists())
    referenced = await session.scalar(select(func.min(event.sequence)).where(
        event.loop_id == loop_id, event.sequence < boundary, or_(*references)))
    if referenced is not None:
        boundary = min(boundary, referenced)
    for model, field in (
        (LoopUserIntent, LoopUserIntent.request_payload["visible_context"]["live_sequence"]),
        (LoopObservation, LoopObservation.envelope["intervention"]["visible_context"]["live_sequence"]),
        (LoopObservation, LoopObservation.envelope["pre_input_state"]["intervention"]["visible_context"]["live_sequence"]),
    ):
        visible = await session.scalar(select(func.min(field.as_integer())).where(model.loop_id == loop_id))
        if visible is not None and visible > 0:
            boundary = min(boundary, visible)
    return int(boundary)
