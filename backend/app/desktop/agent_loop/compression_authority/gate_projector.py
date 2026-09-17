r"""本文件对外提供 LoopCompressionGateProjector 的稳定 Run 结算投影。

输入为已持久化 MainRunSettled event、DesktopRun、Loop 与当前事务；输出为精确绑定 Run、Context、
revision、checkpoint、round、authority 和 goal 的 common pending decision，或无压缩 gate 时返回 None。
具体工作流为只在 Run 终态且主图仍有 compression_request 时读取恢复投影，再复用通用 projector 的
幂等 identity 和安全默认授权。示例：`await projector.project_settled(session, event, run, loop)`。
"""

from __future__ import annotations

from typing import Any

from backend.app.desktop.agent_loop.gates import PendingDecisionContract, PendingDecisionProjector
from backend.app.desktop.compression import compression_recovery_payload
from backend.app.desktop.models import DesktopThread


class LoopCompressionGateProjector:
    def __init__(self, checkpointer: Any, pending: PendingDecisionProjector) -> None:
        self._checkpointer = checkpointer
        self._pending = pending

    async def project_settled(self, session, event, run, loop) -> PendingDecisionContract | None:
        if not run.round_id or run.round_id != loop.current_round_id:
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
                "authority_revision": loop.authority_revision,
                "goal_revision": loop.goal_revision,
            },
        )
