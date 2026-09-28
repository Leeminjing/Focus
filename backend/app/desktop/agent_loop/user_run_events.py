r"""本文件对外提供 LoopUserRunEventRecorder。

输入为直接用户 Loop intent（首轮初始 Run 可为空）、Run 身份和可选结算事件；输出为可重放的 Context Run 启动与结算里程碑。
具体工作流为使用 Run identity 生成稳定幂等键，只把状态和安全摘要写入 Loop journal，保证首轮和后续无 Directive 的直接用户 Run 都能进入当前执行投影。
示例：`await recorder.started(session, intent, run)`。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.models import LoopUserIntent
from backend.app.desktop.models import DesktopRun


class LoopUserRunEventRecorder:
    def __init__(self) -> None:
        self._journal = LoopEventJournal()

    async def started(self, session: AsyncSession, intent: LoopUserIntent | None, run: DesktopRun) -> None:
        await self._journal.append(
            session, run.loop_id,
            CanonicalEventDraft(
                kind="context.run.started",
                entity_type="context_run",
                entity_id=run.run_id,
                entity_revision=1,
                correlation_id=intent.correlation_id if intent is not None else f"initial-run:{run.run_id}",
                payload={
                    "run_id": run.run_id,
                    "context_id": run.task_id,
                    "status": "running",
                    "summary": "用户 Context Run 已启动",
                },
                idempotency_key=f"context-run:{run.run_id}:started",
            ),
        )

    async def settled(self, session: AsyncSession, intent: LoopUserIntent | None, run: DesktopRun, cause_event_id: str | None) -> None:
        await self._journal.append(
            session, run.loop_id,
            CanonicalEventDraft(
                kind="context.run.settled",
                entity_type="context_run",
                entity_id=run.run_id,
                entity_revision=2,
                correlation_id=intent.correlation_id if intent is not None else f"initial-run:{run.run_id}",
                causation_id=cause_event_id,
                payload={
                    "run_id": run.run_id,
                    "context_id": run.task_id,
                    "status": run.status,
                    "summary": f"用户 Context Run 已进入 {run.status}",
                },
                idempotency_key=f"context-run:{run.run_id}:settled",
            ),
        )
