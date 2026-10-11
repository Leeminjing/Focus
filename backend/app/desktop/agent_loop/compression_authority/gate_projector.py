r"""本文件对外提供 LoopCompressionGateProjector 的稳定 Run 结算投影。

输入为已持久化 MainRunSettled event、DesktopRun、Loop 与当前事务；输出为精确绑定 Run、Context、
revision、checkpoint、round、authority 和 goal 的 common pending decision，或无压缩 gate 时返回 None。
具体工作流为只在 Run 终态且主图仍有 compression_request 时读取恢复投影，再复用通用 projector 的
幂等 identity 和安全默认授权。示例：`await projector.project_settled(session, event, run, loop)`。
重叠轮仍按 Run 所属 Round 与已提交 Directive 固定授权/Mission 来源，较新轮不丢弃旧执行产生的人工 gate。
"""

from __future__ import annotations

from typing import Any

from backend.app.desktop.agent_loop.gates import PendingDecisionContract, PendingDecisionProjector
from backend.app.desktop.agent_loop.models import LoopDecision, LoopDirective, LoopRound
from backend.app.desktop.compression import compression_recovery_payload
from backend.app.desktop.models import DesktopThread


class LoopCompressionGateProjector:
    def __init__(self, checkpointer: Any, pending: PendingDecisionProjector) -> None:
        self._checkpointer = checkpointer
        self._pending = pending

    async def project_settled(self, session, event, run, loop) -> PendingDecisionContract | None:
        if not run.round_id or run.loop_id != loop.loop_id:
            return None
        round_row = await session.get(LoopRound, run.round_id)
        directive = await session.get(LoopDirective, run.directive_id) if run.directive_id else None
        decision = await session.get(LoopDecision, round_row.decision_id) if round_row and round_row.decision_id else None
        if (round_row is None or round_row.loop_id != loop.loop_id or directive is None or decision is None
                or directive.round_id != run.round_id or directive.decision_id != decision.decision_id
                or decision.authority_committed_at is None):
            return None
        task = await session.get(DesktopThread, run.task_id)
        if task is None:
            return None
        recovery = await compression_recovery_payload(session, task, self._checkpointer)
        if recovery is None:
            return None
        revision = (event.payload or {}).get("context_revision") or {}
        return await self._pending.project_in_session(
            session,
            loop.loop_id,
            {
                "type": "compression",
                "request": recovery.get("request") or {},
                "recovery_status": recovery.get("status"),
                "delegation_blocked": recovery.get("status") != "resumable",
                "run_id": run.run_id,
                "origin_message_id": run.origin_message_id,
                "context_id": run.task_id,
                "context_revision_id": revision.get("revision_id") or run.context_revision_id,
                "checkpoint_id": revision.get("checkpoint_id") or run.final_checkpoint_id or run.context_checkpoint_id,
                "execution_thread_id": run.execution_thread_id,
                "checkpoint_ns": run.checkpoint_ns,
                "round_id": run.round_id,
                "authority_revision": directive.grant_revision,
                "goal_revision": round_row.goal_revision,
            },
        )
