r"""本文件对外提供 LoopTerminalLifecycle 与 TerminalFinalizationResult 统一提交 Loop 终态。

输入为调用方事务中已锁定的 AgentLoop、completed/stopped/failed 目标、原因和可选最终结果；输出为被取消的 Run 与已释放 Lane identity。
具体工作流为设置终态字段、撤销活动 grant、收敛运行时工作、停止压缩授权、释放策展所有权并追加规范终态事件，任一步失败均由调用方事务整体回滚。
示例：`result = await lifecycle.finalize(session, loop, "stopped", "user_stop")`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.compression_authority.repository import CompressionAuthorityRepository
from backend.app.desktop.agent_loop.curation_ownership import CurationOwnershipRepository, TERMINAL_LOOP_STATUSES
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant
from backend.app.desktop.agent_loop.runtime_convergence import LoopRuntimeConvergence


@dataclass(frozen=True, slots=True)
class TerminalFinalizationResult:
    status: str
    cancelled_run_ids: tuple[str, ...]
    retired_lane_ids: tuple[str, ...]


class LoopTerminalLifecycle:
    def __init__(
        self,
        convergence: LoopRuntimeConvergence | None = None,
        ownership: CurationOwnershipRepository | None = None,
        journal: LoopEventJournal | None = None,
    ) -> None:
        self._convergence = convergence or LoopRuntimeConvergence()
        self._ownership = ownership or CurationOwnershipRepository()
        self._journal = journal or LoopEventJournal()

    async def finalize(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        status: str,
        reason: str,
        *,
        final_result: dict | None = None,
    ) -> TerminalFinalizationResult:
        if status not in TERMINAL_LOOP_STATUSES:
            raise ValueError(f"不支持的 Loop 终态: {status}")
        now = datetime.now(UTC)
        loop.status = status
        loop.health = "idle"
        loop.waiting_reason = None
        loop.completed_at = loop.completed_at or now
        if final_result is not None:
            loop.final_result = final_result
        await self._revoke_active_grants(session, loop.loop_id, now)
        await CompressionAuthorityRepository().supersede(session, loop.loop_id, reason)
        cancelled = await self._convergence.converge(session, loop, reason)
        retired = await self._ownership.release_terminal(session, loop, reason=reason)
        await self._record_terminal_event(session, loop, reason, retired)
        return TerminalFinalizationResult(status, cancelled, retired)

    @staticmethod
    async def _revoke_active_grants(session: AsyncSession, loop_id: str, now: datetime) -> None:
        grants = tuple(
            (
                await session.scalars(
                    select(LoopDelegationGrant)
                    .where(LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.status == "active")
                    .with_for_update()
                )
            ).all()
        )
        for grant in grants:
            grant.status = "revoked"
            grant.revoked_at = now

    async def _record_terminal_event(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        reason: str,
        retired_lane_ids: tuple[str, ...],
    ) -> None:
        await self._journal.append(
            session,
            loop.loop_id,
            CanonicalEventDraft(
                kind="loop.lifecycle.terminal",
                entity_type="loop",
                entity_id=loop.loop_id,
                entity_revision=loop.revision,
                correlation_id=loop.loop_id,
                payload={
                    "loop_id": loop.loop_id,
                    "status": loop.status,
                    "reason": reason,
                    "retired_lane_ids": list(retired_lane_ids),
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
                idempotency_key=f"loop-terminal:{loop.loop_id}:{loop.revision}:{loop.status}",
            ),
        )
