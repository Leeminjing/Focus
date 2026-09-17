r"""本文件对外提供 round 的创建、无进展判定、终态收敛与停滞候选筛选。

输入为已锁定的 AgentLoop/LoopRound、权威 Workspace Slot、租约事实与轮内进度计数；输出为绑定当前
authority、goal 与 workspace revision 的新 LoopRound，或把 round 与 loop 收敛到终态并追加幂等终结事件的
确定性状态转移。具体工作流为：create_observation_round 读取权威 Workspace Slot 并分配单调轮号；
stall_reasons 依轮内尝试次数、同状态停留时长与无进展计数判定越界；terminate_round 只收敛调用方显式声明
前置状态的 round（置为 error，仅当其仍是 loop 当前轮时把 loop 交回用户，并追加 round-terminated 事件）；
select_stalled_rounds 在排除仍被有效租约持有的候选后给出可收敛集合。本模块只做状态转移与只读筛选，
不提交事务。示例：`await terminate_round(session, loop, round_row, category="rejected", reason="...", allowed_statuses=UNDECIDED_ROUND_STATUSES)`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from collections.abc import Collection
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopCoordinatorLease,
    LoopDecision,
    LoopEventOutbox,
    LoopPatrolAttempt,
    LoopRound,
)
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


CLAIMABLE_ROUND_STATUSES = ("observed", "publishing", "adopting", "ready")
STALL_SCAN_ROUND_STATUSES = ("observed", "publishing", "adopting")
UNDECIDED_ROUND_STATUSES = ("observed",)
TERMINAL_ROUND_STATUSES = frozenset({"settled", "error", "superseded"})
TERMINATION_EVENT = "RoundTerminated"
_TERMINATION_KEY = "round-terminated:{round_id}"
_WAITING_REASON_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class RoundStallLimits:
    """round 无进展界限：任一维度越界即视为停滞，可被运行期看门狗与启动恢复收敛。"""

    max_round_seconds: int = 1800
    max_patrol_attempts: int = 12
    max_no_progress: int = 5


@dataclass(frozen=True, slots=True)
class StalledRound:
    round_id: str
    loop_id: str
    reasons: tuple[str, ...]
    decision_id: str | None = None


async def select_stalled_rounds(session: AsyncSession, limits: RoundStallLimits, now: datetime) -> list[StalledRound]:
    """筛选已不可能推进的 round：已有落定 decision，或越过任一无进展维度且未被有效租约持有。"""
    rounds = list(
        (
            await session.scalars(
                select(LoopRound)
                .join(AgentLoop, AgentLoop.loop_id == LoopRound.loop_id)
                .outerjoin(LoopCoordinatorLease, LoopCoordinatorLease.round_id == LoopRound.round_id)
                .where(
                    AgentLoop.status == "running",
                    LoopRound.status.in_(STALL_SCAN_ROUND_STATUSES),
                    or_(LoopCoordinatorLease.lease_id.is_(None), LoopCoordinatorLease.expires_at <= now),
                )
                .order_by(LoopRound.started_at)
            )
        ).all()
    )
    if not rounds:
        return []
    round_ids = [row.round_id for row in rounds]
    attempts = await _attempt_counts(session, round_ids)
    decided = await _decided_decisions(session, round_ids)
    progress = await _no_progress_counts(session, [row.loop_id for row in rounds])
    stalled: list[StalledRound] = []
    for row in rounds:
        if row.round_id in decided:
            stalled.append(StalledRound(row.round_id, row.loop_id, ("round_already_decided",), decided[row.round_id]))
            continue
        reasons = stall_reasons(
            status=row.status,
            started_at=row.started_at,
            attempts=attempts.get(row.round_id, 0),
            no_progress_count=progress.get(row.loop_id, 0),
            now=now,
            limits=limits,
        )
        if reasons:
            stalled.append(StalledRound(row.round_id, row.loop_id, reasons))
    return stalled


def stall_reasons(
    *,
    status: str,
    started_at: datetime,
    attempts: int,
    no_progress_count: int,
    now: datetime,
    limits: RoundStallLimits,
) -> tuple[str, ...]:
    """纯判定：返回越界维度名元组；空元组表示本 round 仍可能自行推进。"""
    reasons: list[str] = []
    if status == "observed" and attempts >= limits.max_patrol_attempts:
        reasons.append("patrol_attempts")
    if _age_seconds(started_at, now) >= limits.max_round_seconds:
        reasons.append("round_seconds")
    if attempts >= 1 and no_progress_count >= limits.max_no_progress:
        reasons.append("no_progress")
    return tuple(reasons)


async def terminate_stalled_rounds(
    session: AsyncSession,
    limits: RoundStallLimits,
    now: datetime,
    *,
    category: str,
) -> list[str]:
    """收敛停滞 round：逐轮加锁重查，仍在有效租约下的候选一律不动。"""
    terminated: list[str] = []
    for candidate in await select_stalled_rounds(session, limits, now):
        round_row = await session.get(LoopRound, candidate.round_id, with_for_update=True)
        if round_row is None or round_row.status not in STALL_SCAN_ROUND_STATUSES:
            continue
        if await _has_live_lease(session, candidate.round_id, now):
            continue
        loop = await session.get(AgentLoop, candidate.loop_id, with_for_update=True)
        terminated_now = await terminate_round(
            session,
            loop,
            round_row,
            category=category,
            reason=_stall_reason_text(candidate.reasons),
            decision_id=candidate.decision_id,
            allowed_statuses=STALL_SCAN_ROUND_STATUSES,
        )
        if terminated_now:
            terminated.append(candidate.round_id)
    return terminated


def _stall_reason_text(reasons: tuple[str, ...]) -> str:
    if "round_already_decided" in reasons:
        return "Round 已有落定决策却仍停留在可领取状态，已收敛为终态"
    return f"Round 无进展（{'、'.join(reasons)}），已收敛为终态"


async def terminate_round(
    session: AsyncSession,
    loop: AgentLoop | None,
    round_row: LoopRound,
    *,
    category: str,
    reason: str,
    allowed_statuses: Collection[str],
    decision_id: str | None = None,
    wait_for_user: bool = True,
) -> bool:
    """把 round 收敛为终态；仅当它仍是 loop 当前轮时才把 loop 交回用户并记录原因。

    `allowed_statuses` 由调用方显式声明本次收敛的前置状态，避免失败路径误伤已提交权威事实的 round。
    """
    if round_row.status in TERMINAL_ROUND_STATUSES or round_row.status not in allowed_statuses:
        return False
    round_row.status = "error"
    round_row.settled_at = datetime.now(UTC)
    if decision_id is not None:
        round_row.decision_id = decision_id
    if wait_for_user and loop is not None and loop.status == "running" and loop.current_round_id == round_row.round_id:
        loop.status = "waiting_user"
        loop.health = "degraded"
        loop.waiting_reason = reason[:_WAITING_REASON_LIMIT]
    await _append_termination_event(session, round_row.loop_id, round_row.round_id, category, reason)
    return True


async def create_observation_round(
    session: AsyncSession,
    loop: AgentLoop,
    prior: LoopRound | None,
    fallback_frontier_hash: str,
) -> LoopRound:
    slot = await session.scalar(
        select(WorkspaceSlot).where(
            WorkspaceSlot.workspace_id == loop.workspace_id,
            WorkspaceSlot.kind == "authoritative",
            WorkspaceSlot.lifecycle != "deleted",
        )
    )
    number = int(
        await session.scalar(select(func.max(LoopRound.number)).where(LoopRound.loop_id == loop.loop_id))
        or 0
    ) + 1
    return LoopRound(
        round_id=uuid.uuid4().hex,
        loop_id=loop.loop_id,
        number=number,
        authority_revision=loop.authority_revision,
        goal_revision=loop.goal_revision,
        frontier_hash=prior.frontier_hash if prior else fallback_frontier_hash,
        workspace_revision=slot.revision if slot else 1,
    )


async def _append_termination_event(session: AsyncSession, loop_id: str, round_id: str, category: str, reason: str) -> None:
    sequence = int(
        await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop_id))
        or 0
    ) + 1
    session.add(
        LoopEventOutbox(
            event_id=uuid.uuid4().hex,
            loop_id=loop_id,
            sequence=sequence,
            event_type=TERMINATION_EVENT,
            payload={"round_id": round_id, "category": category, "reason": reason},
            idempotency_key=_TERMINATION_KEY.format(round_id=round_id),
        )
    )


async def _attempt_counts(session: AsyncSession, round_ids: list[str]) -> dict[str, int]:
    rows = await session.execute(
        select(LoopPatrolAttempt.round_id, func.count())
        .where(LoopPatrolAttempt.round_id.in_(round_ids))
        .group_by(LoopPatrolAttempt.round_id)
    )
    return {round_id: int(count) for round_id, count in rows.all()}


async def _decided_decisions(session: AsyncSession, round_ids: list[str]) -> dict[str, str]:
    rows = await session.execute(select(LoopDecision.round_id, LoopDecision.decision_id).where(LoopDecision.round_id.in_(round_ids)))
    return {round_id: decision_id for round_id, decision_id in rows.all()}


async def _no_progress_counts(session: AsyncSession, loop_ids: list[str]) -> dict[str, int]:
    rows = await session.execute(
        select(LoopBudgetUsage.loop_id, LoopBudgetUsage.no_progress_count).where(LoopBudgetUsage.loop_id.in_(loop_ids))
    )
    return {loop_id: int(count or 0) for loop_id, count in rows.all()}


async def _has_live_lease(session: AsyncSession, round_id: str, now: datetime) -> bool:
    lease = await session.scalar(
        select(LoopCoordinatorLease).where(
            LoopCoordinatorLease.round_id == round_id,
            LoopCoordinatorLease.expires_at > now,
        )
    )
    return lease is not None


def _age_seconds(started_at: datetime, now: datetime) -> int:
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return max(0, int((now - started_at).total_seconds()))
