r"""本文件对外提供 DirectiveCausalityRecorder 与 DirectiveCausalityQuery。

输入为已持久 Directive/Run/workspace 结果和稳定 correlation identity；输出为包含真实 Directive 当前动作的 Run/Workspace 规范事件及按 sequence
排序的完整因果查询。具体工作流为启动和结算边界原子追加事件，查询读取 directive transition、Run、anchor 与同 correlation
journal；后续 Tool/Fact projector 复用同一 correlation 即自动进入链。示例：`chain = await query.read(session, directive_id)`。
统一 Run 因果事件同时公开真实 origin、user_intent_id、所属轮文件授权和结算状态，用户交付与 Patrol 委托可区分且不重复生产事件。
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.models import LoopDecision, LoopDirective, LoopDirectiveTransition
from backend.app.desktop.agent_loop.directive_equipment import round_permission_state
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceSlot


class DirectiveCausalityRecorder:
    def __init__(self) -> None:
        self._journal = LoopEventJournal()

    async def run_started(self, session: AsyncSession, directive: LoopDirective, run_id: str) -> None:
        run = await session.get(DesktopRun, run_id)
        await self._journal.append(
            session,
            directive.loop_id,
            CanonicalEventDraft(
                kind="context.run.started",
                entity_type="context_run",
                entity_id=run_id,
                entity_revision=1,
                correlation_id=directive.correlation_id,
                payload={
                    "run_id": run_id,
                    "directive_id": directive.directive_id,
                    "round_id": directive.round_id,
                    "context_id": directive.target_context_id,
                    "origin": directive.origin_kind,
                    "user_intent_id": directive.correlation_id if directive.origin_kind == "direct_user" else None,
                    "status": "running",
                    "settlement_status": "pending",
                    "round_authorization": (run.equipment or {}).get("round_authorization") if run else None,
                    "permission_state": await round_permission_state(session, run) if run else None,
                    "current_action": directive.content,
                    "summary": "Context 已按授权 Directive 启动",
                },
                idempotency_key=f"context-run:{run_id}:started",
            ),
        )

    async def run_settled(self, session: AsyncSession, directive: LoopDirective, run: DesktopRun, event_id: str | None) -> None:
        await self._journal.append(
            session,
            directive.loop_id,
            CanonicalEventDraft(
                kind="context.run.settled",
                entity_type="context_run",
                entity_id=run.run_id,
                entity_revision=2,
                correlation_id=directive.correlation_id,
                causation_id=event_id,
                payload={
                    "run_id": run.run_id,
                    "directive_id": directive.directive_id,
                    "round_id": directive.round_id,
                    "context_id": run.task_id,
                    "origin": run.origin,
                    "user_intent_id": run.user_intent_id,
                    "status": run.status,
                    "settlement_status": "settled" if run.settled_at else "pending",
                    "round_authorization": (run.equipment or {}).get("round_authorization"),
                    "permission_state": await round_permission_state(session, run),
                    "error": run.error,
                },
                idempotency_key=f"context-run:{run.run_id}:settled",
            ),
        )
        if run.workspace_result:
            await self._journal.append(
                session,
                directive.loop_id,
                CanonicalEventDraft(
                    kind="context.workspace.changed",
                    entity_type="workspace_change",
                    entity_id=f"{run.run_id}:workspace",
                    entity_revision=1,
                    correlation_id=directive.correlation_id,
                    causation_id=event_id,
                    payload={
                        "run_id": run.run_id,
                        "directive_id": directive.directive_id,
                        "workspace_result": run.workspace_result,
                    },
                    idempotency_key=f"context-run:{run.run_id}:workspace",
                ),
            )


class DirectiveCausalityQuery:
    async def read(self, session: AsyncSession, directive_id: str) -> dict:
        directive = await session.get(LoopDirective, directive_id)
        if directive is None:
            raise LookupError("Directive 不存在")
        decision = await session.get(LoopDecision, directive.decision_id)
        transitions = tuple(
            (
                await session.scalars(
                    select(LoopDirectiveTransition)
                    .where(LoopDirectiveTransition.directive_id == directive_id)
                    .order_by(LoopDirectiveTransition.revision)
                )
            ).all()
        )
        runs = tuple(
            (
                await session.scalars(
                    select(DesktopRun)
                    .where(DesktopRun.directive_id == directive_id)
                    .order_by(DesktopRun.created_at, DesktopRun.run_id)
                )
            ).all()
        )
        run_ids = tuple(run.run_id for run in runs)
        anchors = () if not run_ids else tuple(
            (
                await session.execute(
                    select(RunExecutionAnchor, WorkspaceSlot)
                    .join(WorkspaceSlot, WorkspaceSlot.slot_id == RunExecutionAnchor.slot_id)
                    .where(RunExecutionAnchor.run_id.in_(run_ids))
                    .order_by(RunExecutionAnchor.created_at, RunExecutionAnchor.run_id)
                )
            ).all()
        )
        events = tuple(
            (
                await session.scalars(
                    select(LoopJournalEvent)
                    .where(
                        LoopJournalEvent.loop_id == directive.loop_id,
                        or_(
                            LoopJournalEvent.correlation_id == directive.correlation_id,
                            (LoopJournalEvent.entity_type == "directive") & (LoopJournalEvent.entity_id == directive_id),
                        ),
                    )
                    .order_by(LoopJournalEvent.sequence)
                )
            ).all()
        )
        return {
            "directive": {
                "directive_id": directive.directive_id,
                "origin": directive.origin_kind,
                "target_context_id": directive.target_context_id,
                "state": directive.lifecycle_state,
                "correlation_id": directive.correlation_id,
                "loop_id": directive.loop_id,
                "round_id": directive.round_id,
                "content": directive.content,
                "reason": directive.terminal_reason or directive.queued_reason,
                "causation_event_id": directive.causation_event_id,
            },
            "decision": None if decision is None else {
                "decision_id": decision.decision_id,
                "rationale": decision.rationale,
                "evidence": decision.evidence,
            },
            "transitions": tuple({"revision": item.revision, "from_state": item.from_state, "state": item.to_state, "run_id": item.run_id, "reason": item.reason, "caused_by_event_id": item.caused_by_event_id, "occurred_at": item.occurred_at.isoformat()} for item in transitions),
            "runs": tuple({"run_id": item.run_id, "status": item.status, "origin": item.origin} for item in runs),
            "workspace": tuple({
                "run_id": anchor.run_id, "slot_id": anchor.slot_id,
                "root_path": slot.root_path, "kind": slot.kind,
                "observed_workspace_revision": anchor.observed_workspace_revision,
                "resulting_workspace_revision": anchor.resulting_workspace_revision,
                "observed_fingerprint": anchor.observed_fingerprint,
                "resulting_fingerprint": anchor.resulting_fingerprint,
                "adoption_state": anchor.adoption_state,
                "effect_evidence": anchor.effect_evidence,
            } for anchor, slot in anchors),
            "events": tuple({"event_id": item.event_id, "sequence": item.sequence, "kind": item.kind, "entity_type": item.entity_type, "entity_id": item.entity_id, "correlation_id": item.correlation_id, "causation_id": item.causation_id, "payload": item.payload} for item in events),
        }
