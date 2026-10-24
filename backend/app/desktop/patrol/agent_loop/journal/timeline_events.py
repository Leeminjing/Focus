"""Capture the public decision and its actual input links at authority commit."""
from sqlalchemy import select

from backend.app.desktop.patrol.agent_loop.journal.event_contract import CanonicalEventDraft
from backend.app.desktop.patrol.agent_loop.journal.event_journal import LoopEventJournal
from backend.app.desktop.patrol.agent_loop.models import LoopUserIntent


async def record_work_decision(session, decision):
    intent_ids = list(await session.scalars(select(LoopUserIntent.intent_id).where(
        LoopUserIntent.loop_id == decision.loop_id,
        LoopUserIntent.observed_round_id == decision.round_id,
    ).order_by(LoopUserIntent.intent_id)))
    actions = decision.intent.get("actions") or []
    await LoopEventJournal().append(session, decision.loop_id, CanonicalEventDraft(
        kind="patrol.decision.committed", entity_type="decision", entity_id=decision.decision_id,
        entity_revision=1, correlation_id=decision.round_id,
        payload={"decision_id": decision.decision_id, "round_id": decision.round_id,
                 "summary": decision.rationale[:2000], "intent_ids": intent_ids,
                 "context_ids": sorted({item["context_id"] for item in actions if item.get("context_id")}),
                 "action_types": [item.get("action") for item in actions],
                 "occurred_at": decision.authority_committed_at.isoformat()},
        idempotency_key=f"timeline-decision:{decision.decision_id}",
    ))
