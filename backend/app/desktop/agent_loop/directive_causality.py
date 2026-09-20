r"""本文件对外提供 DirectiveCausalityRecorder 与 DirectiveCausalityQuery。

输入为已持久 Directive/Run/workspace 结果和稳定 correlation identity；输出为包含真实 Directive 当前动作的 Run/Workspace 规范事件及按 sequence
排序的完整因果查询。具体工作流为启动和结算边界原子追加事件，查询读取 directive transition、Run、anchor 与同 correlation
journal；后续 Tool/Fact projector 复用同一 correlation 即自动进入链。示例：`chain = await query.read(session, directive_id)`。
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.agent_loop.models import LoopDirective, LoopDirectiveTransition
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor


class DirectiveCausalityRecorder:
    def __init__(self) -> None:
        self._journal = LoopEventJournal()

    async def run_started(self, session: AsyncSession, directive: LoopDirective, run_id: str) -> None:
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
                    "status": "running",
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
                    "status": run.status,
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
                await session.scalars(
                    select(RunExecutionAnchor).where(RunExecutionAnchor.run_id.in_(run_ids))
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
            },
            "transitions": tuple({"revision": item.revision, "state": item.to_state, "run_id": item.run_id, "reason": item.reason} for item in transitions),
            "runs": tuple({"run_id": item.run_id, "status": item.status, "origin": item.origin} for item in runs),
            "workspace": tuple({"run_id": item.run_id, "slot_id": item.slot_id, "effect_evidence": item.effect_evidence} for item in anchors),
            "events": tuple({"sequence": item.sequence, "kind": item.kind, "entity_type": item.entity_type, "entity_id": item.entity_id, "payload": item.payload} for item in events),
        }
