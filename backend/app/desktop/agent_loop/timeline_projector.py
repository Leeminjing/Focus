"""Project committed journal events into durable work history, without writing facts.

Each bounded page and its cursor commit atomically. Failed pages retry in place;
other Loops still progress. Retained history is never joined back to the journal.
"""
from datetime import UTC, datetime
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent, LoopJournalSequence, LoopProjectorCursor, LoopReplayRetention
from backend.app.desktop.agent_loop.models import LoopDecision, LoopDirective, LoopRound, LoopUserIntent
from backend.app.desktop.agent_loop.timeline_models import WorkTimelineEntry
from backend.app.desktop.models import DesktopRun

logger = logging.getLogger(__name__)


class WorkTimelineProjector:
    PROJECTOR_NAME = "work_timeline"

    def __init__(self, sessions, page_size=100):
        self._sessions = sessions
        self._page_size = max(1, min(page_size, 500))

    async def drain(self):
        async with self._sessions() as session:
            loops = tuple(await session.scalars(select(LoopJournalSequence.loop_id).outerjoin(
                LoopProjectorCursor,
                (LoopProjectorCursor.loop_id == LoopJournalSequence.loop_id)
                & (LoopProjectorCursor.projector_name == self.PROJECTOR_NAME),
            ).where((LoopProjectorCursor.last_sequence.is_(None))
                    | (LoopProjectorCursor.last_sequence < LoopJournalSequence.last_sequence))))
        processed = 0
        for loop_id in loops:
            try:
                processed += await self.project_loop(loop_id)
            except Exception:
                logger.exception("Work timeline projection failed for Loop %s; cursor retained", loop_id)
        return processed

    async def project_loop(self, loop_id):
        processed = 0
        # Bound one visit so a busy Loop cannot starve others.
        for _ in range(10):
            count = await self._page(loop_id)
            processed += count
            if count < self._page_size:
                break
        return processed

    async def _page(self, loop_id):
        async with self._sessions.begin() as session:
            await session.execute(insert(LoopProjectorCursor).values(
                loop_id=loop_id, projector_name=self.PROJECTOR_NAME, last_sequence=0,
            ).on_conflict_do_nothing())
            cursor = await session.get(LoopProjectorCursor, (loop_id, self.PROJECTOR_NAME), with_for_update=True)
            retention = await session.get(LoopReplayRetention, loop_id)
            minimum = retention.minimum_sequence if retention else 1
            if cursor.last_sequence < minimum - 1:
                # An old installation may already have pruned its journal. Record
                # a real gap, never a reconstructed ordering of current entities.
                await self._save(session, dict(
                    loop_id=loop_id, source_event_id=f"gap:{minimum}", sequence=minimum - 1,
                    kind="timeline.history_gap", category="system", title="早期历史不可用",
                    summary="部分源事件已在时间线启用前清理，无法还原其工作顺序。",
                    context_ids=[], references={"missing_after": cursor.last_sequence, "missing_before": minimum},
                    visibility={}, recorded_at=datetime.now(UTC),
                ))
                cursor.last_sequence = minimum - 1
            events = tuple(await session.scalars(select(LoopJournalEvent).where(
                LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.sequence > cursor.last_sequence,
            ).order_by(LoopJournalEvent.sequence).limit(self._page_size)))
            for event in events:
                values = await self._entry(session, event)
                if values is not None:
                    await self._save(session, values)
                cursor.last_sequence = event.sequence
            return len(events)

    @staticmethod
    async def _save(session, values):
        entry_id = uuid.uuid5(uuid.NAMESPACE_URL, f"timeline:{values['loop_id']}:{values['source_event_id']}").hex
        await session.execute(insert(WorkTimelineEntry).values(entry_id=entry_id, **values).on_conflict_do_nothing())

    async def _entry(self, session, event):
        p, kind = event.payload, event.kind
        refs = {}
        category, title, summary = "", "", ""
        round_id = p.get("round_id")
        context_ids = list(p.get("context_ids") or [])
        context_ids += [p[key] for key in ("context_id", "source_context_id", "target_context_id") if p.get(key)]
        run_id = p.get("run_id")
        fact_id = revision = intent_id = decision_id = None
        occurred_at = None

        if kind == "fact.upserted":
            if p.get("fact_type") == "tool":
                return None
            presentation = p.get("presentation") or {}
            category, fact_id, revision = "fact", p.get("fact_id") or event.entity_id, p.get("revision") or event.entity_revision
            state = p.get("state")
            title = str(presentation.get("title") or p.get("fact_type") or "事实记录")
            summary = str(presentation.get("summary") or "")
            refs = {"fact_type": p.get("fact_type"), "state": state, "outcome_status": presentation.get("outcome_status")}
            if presentation.get("file"):
                refs["file"] = presentation["file"]
            refs["change"] = "observed" if revision == 1 else "revision"
            # A verification has its own recorded time and the original fact's
            # occurrence time; it is explicitly a revision, never a second run.
            if revision > 1:
                labels = {"verifying": "开始核验", "verified": "验证通过", "contradicted": "已被反证", "superseded": "已被替代"}
                title = f"{labels.get(state, '事实修订')} · {title}"
            context_ids += [p["source_context_id"]] if p.get("source_context_id") else []
            run_id = p.get("source_run_id")
        elif kind.startswith("intervention."):
            state = kind.removeprefix("intervention.")
            if state not in {"submitted", "observed", "addressed", "rejected", "cancelled"}:
                return None
            category, intent_id = "user_decision", p.get("intent_id") or event.entity_id
            title = {"submitted": "用户提出新想法", "observed": "Patrol 接收用户输入", "addressed": "Patrol 已处理用户输入", "rejected": "用户输入未被采纳", "cancelled": "用户输入已取消"}[state]
            intent = await session.get(LoopUserIntent, intent_id)
            if intent is not None and intent.loop_id == event.loop_id:
                summary = p.get("content") or (intent.content if state == "submitted" else p.get("reason") or "")
                # Submitted intents do not yet belong to a Round. Only the
                # observation/handling transition establishes that association.
                round_id = p.get("round_id")
                if state == "submitted":
                    occurred_at = intent.created_at
            else:
                summary = p.get("content") or p.get("reason") or ""
        elif kind == "patrol.decision.committed":
            category, title, summary = "user_decision", "Patrol 提交工作决策", p.get("summary") or ""
            decision_id = p.get("decision_id") or event.entity_id
            refs = {"intent_ids": p.get("intent_ids") or [], "action_types": p.get("action_types") or []}
        elif kind in {"context.created", "context.lineage.derived"}:
            category = "context"
            title = "Context 自动派生" if kind.endswith("derived") else "创建 Context"
            summary = p.get("title") or ""
            if kind == "context.created":
                context_ids.append(event.entity_id)
            # These events use the publishing decision as correlation, not Round.
            decision = await session.get(LoopDecision, event.correlation_id) if event.correlation_id else None
            if decision is not None and decision.loop_id == event.loop_id:
                round_id, decision_id = decision.round_id, decision.decision_id
            refs = {key: p[key] for key in ("source_context_id", "target_context_id", "source_revision_id", "target_revision_id") if key in p}
            labels = p.get("context_labels") or {}
            refs["context_labels"] = labels
            if kind.endswith("derived") and labels:
                summary = f"{labels.get(p.get('source_context_id'), p.get('source_context_id'))} → {labels.get(p.get('target_context_id'), p.get('target_context_id'))}"
        elif kind == "context.run.settled":
            category, title = "run", "Agent 执行结束"
            run_id = run_id or event.entity_id
            summary = p.get("summary") or str(p.get("status") or "")
            refs = {"status": p.get("status")}
        elif kind == "loop.round.state_changed":
            category, title, round_id = "round", "Round 状态更新", p.get("round_id") or event.entity_id
            summary = str(p.get("status") or "")
            decision_id = p.get("decision_id")
            refs = {"status": p.get("status"), "observation_id": p.get("observation_id")}
            # Older journals have no dedicated decision event. A state envelope
            # explicitly witnessing authority commit can anchor that decision;
            # a current decision pointer alone must never invent a past commit.
            if decision_id and p.get("authority_committed_at"):
                existing = await session.scalar(select(WorkTimelineEntry.entry_id).where(
                    WorkTimelineEntry.loop_id == event.loop_id,
                    WorkTimelineEntry.decision_id == decision_id,
                    WorkTimelineEntry.category == "user_decision",
                ).limit(1))
                decision = await session.get(LoopDecision, decision_id)
                if existing is None and decision is not None and decision.loop_id == event.loop_id:
                    category, title, summary = "user_decision", "Patrol 提交工作决策", decision.rationale
                    refs["intent_ids"] = list(await session.scalars(select(LoopUserIntent.intent_id).where(
                        LoopUserIntent.loop_id == event.loop_id, LoopUserIntent.observed_round_id == round_id,
                    )))
        else:
            return None

        if run_id:
            run = await session.get(DesktopRun, run_id)
            if run is not None and run.loop_id == event.loop_id:
                round_id = round_id or run.round_id
                context_ids.append(run.task_id)
                intent_id = intent_id or run.user_intent_id
                directive = await session.get(LoopDirective, run.directive_id) if run.directive_id else None
                if directive is not None and directive.loop_id == event.loop_id:
                    decision_id = directive.decision_id
                if kind == "context.run.settled":
                    occurred_at = run.settled_at
        if p.get("occurred_at"):
            try:
                occurred_at = datetime.fromisoformat(p["occurred_at"])
                if occurred_at.tzinfo is None:
                    occurred_at = None
            except (TypeError, ValueError):
                pass
        round_row = await session.get(LoopRound, round_id) if round_id else None
        if round_row is None or round_row.loop_id != event.loop_id:
            round_id, round_number = None, None
        else:
            round_number = round_row.number
        return dict(
            loop_id=event.loop_id, source_event_id=event.event_id, sequence=event.sequence,
            kind=kind, category=category, title=str(title)[:500], summary=str(summary)[:2000],
            round_id=round_id, round_number=round_number, context_ids=sorted(set(context_ids)),
            run_id=run_id, fact_id=fact_id, fact_revision=revision, intent_id=intent_id, decision_id=decision_id,
            correlation_id=event.correlation_id, causation_id=event.causation_id,
            references=refs, visibility=event.visibility, occurred_at=occurred_at, recorded_at=event.occurred_at,
        )
