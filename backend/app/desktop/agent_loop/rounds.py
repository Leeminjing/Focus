r"""本文件对外提供 round 的创建、无进展判定、终态收敛与停滞候选筛选。

输入为已锁定的 AgentLoop/LoopRound、当前活跃 Context revisions、权威 Workspace Slot、租约事实与轮内进度计数；输出为绑定当前
authority、goal 与 workspace revision 的新 LoopRound，或把 round 与 loop 收敛到终态并追加幂等终结事件的
确定性状态转移。具体工作流为：current_frontier_hash 读取活跃 Context 的当前 revisions，create_observation_round
据此读取权威 Workspace Slot 并分配单调轮号；
settle_round 读取该事务全部耐久后果，稳定后幂等收口并计数，活动后果保留身份和等待原因，发布/采用失败转为 error 并交回用户；
stall_reasons 依轮内尝试次数、同状态停留时长与无进展计数判定越界；terminate_round 只收敛调用方显式声明
前置状态的round（默认error，延迟发布可明确superseded，记录结算及同一终结事件；仅当其仍是loop当前轮时交回用户）；
select_stalled_rounds 只把生命周期已终止的决策计为落定（publishing/adopting 表示专职组件仍在推进该决策，
不构成落定），再在排除仍被有效租约持有的候选后给出可收敛集合。本模块只做状态转移与只读筛选，
不提交事务。示例：`await terminate_round(session, loop, round_row, category="rejected", reason="...", allowed_statuses=UNDECIDED_ROUND_STATUSES)`。
收口屏障保存共享后果读取器的历史缺 Decision 诊断，保持原计数和稳定条件。
advance_settled_round 是 Worker/协调者/维护流程共用的唯一推进事务：先 flush，再按 Loop→Round→usage 刷新锁定当前身份，稳定后更新一次语义进展并创建唯一后继轮。
看门狗也先 flush 并按 Loop→Round 刷新锁定，候选读取后控制已暂停时不再终结；有效租约仍优先。
错误收口在调用方同一事务结束该轮仍活动的 Patrol Session，记录失败前的实际 phase、原因分类与冻结身份；终态重放不追加等待或伪造 Decision。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from collections.abc import Collection
from typing import Literal
import hashlib
import json
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopCoordinatorLease,
    LoopContextMembership,
    LoopDecision,
    LoopEventOutbox,
    LoopPatrolAttempt,
    LoopRound,
)
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from backend.app.desktop.models import DesktopThread
from backend.app.desktop.agent_loop.wait_requests import open_recovery_wait


CLAIMABLE_ROUND_STATUSES = ("observed", "curated", "adopting", "ready")
STALL_SCAN_ROUND_STATUSES = ("observed", "curated", "publishing", "adopting")
UNDECIDED_ROUND_STATUSES = ("observed", "curated")
TERMINAL_ROUND_STATUSES = frozenset({"settled", "error", "superseded"})
SETTLED_DECISION_STATUSES = ("committed", "rejected", "superseded")
TERMINATION_EVENT = "RoundTerminated"
_TERMINATION_KEY = "round-terminated:{round_id}"
_WAITING_REASON_LIMIT = 2000


async def settle_round(session: AsyncSession, loop: AgentLoop, round_row: LoopRound) -> bool:
    from backend.app.desktop.agent_loop.round_consequences import RoundConsequenceReader

    if round_row.status == "settled":
        return True
    if round_row.status in {"error", "superseded"}:
        return False
    await session.flush()
    consequences = await RoundConsequenceReader().read(session, round_row)
    round_row.barrier = {**(round_row.barrier or {}), "consequences": consequences.counts,
        "consequence_identities": consequences.identities, "failed_consequences": consequences.failed,
        "diagnostics": consequences.diagnostics}
    if consequences.failed:
        await terminate_round(session, loop, round_row, category="consequence_failure",
            reason="round_consequences_failed:" + ",".join(consequences.failed), allowed_statuses={round_row.status})
        return False
    if not consequences.stable:
        loop.waiting_reason = "round_consequences:" + ",".join(consequences.pending)
        return False
    round_row.status = "settled"
    round_row.settled_at = datetime.now(UTC)
    usage = await session.get(LoopBudgetUsage, loop.loop_id, with_for_update=True)
    if usage is not None and round_row.observation_id and round_row.decision_id:
        usage.rounds += 1
    from backend.app.desktop.agent_loop.round_events import RoundStateEventRecorder

    await RoundStateEventRecorder().record(session, round_row)
    loop.waiting_reason = None
    return True


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
    settled = await _settled_decisions(session, round_ids)
    progress = await _no_progress_counts(session, [row.loop_id for row in rounds])
    stalled: list[StalledRound] = []
    for row in rounds:
        if row.round_id in settled:
            stalled.append(StalledRound(row.round_id, row.loop_id, ("round_already_settled",), settled[row.round_id]))
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
        await session.flush()
        loop = await session.get(AgentLoop, candidate.loop_id, with_for_update=True, populate_existing=True)
        if loop is None or loop.status != 'running':
            continue
        round_row = await session.get(LoopRound, candidate.round_id, with_for_update=True, populate_existing=True)
        if round_row is None or round_row.status not in STALL_SCAN_ROUND_STATUSES:
            continue
        if await _has_live_lease(session, candidate.round_id, now):
            continue
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
    if "round_already_settled" in reasons:
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
    terminal_status: Literal["error", "superseded"] = "error",
) -> bool:
    """把 round 收敛为终态；仅当它仍是 loop 当前轮时才把 loop 交回用户并记录原因。

    `allowed_statuses` 由调用方显式声明本次收敛的前置状态，避免失败路径误伤已提交权威事实的 round。
    """
    if round_row.status in TERMINAL_ROUND_STATUSES or round_row.status not in allowed_statuses:
        return False
    round_row.status = terminal_status
    round_row.settled_at = datetime.now(UTC)
    if decision_id is not None:
        round_row.decision_id = decision_id
    await _terminate_patrol(session, round_row, category, reason)
    if wait_for_user and loop is not None and loop.status == "running" and loop.current_round_id == round_row.round_id:
        loop.health = "degraded"
        await open_recovery_wait(
            session,
            loop,
            reason[:_WAITING_REASON_LIMIT],
            source="round-termination",
            round_id=round_row.round_id,
            scope={"category": category, "decision_id": decision_id},
        )
    from backend.app.desktop.agent_loop.round_events import RoundStateEventRecorder

    await RoundStateEventRecorder().record(session, round_row)
    await _append_termination_event(session, round_row.loop_id, round_row.round_id, category, reason)
    return True


async def advance_settled_round(session: AsyncSession, loop: AgentLoop, round_row: LoopRound) -> LoopRound | None:
    from backend.app.desktop.agent_loop.round_progress import RoundProgressReader
    from backend.app.desktop.agent_loop.lifecycle_events import LoopLifecycleEventRecorder

    await session.flush()
    loop = await session.get(AgentLoop, loop.loop_id, with_for_update=True, populate_existing=True)
    round_row = await session.get(LoopRound, round_row.round_id, with_for_update=True, populate_existing=True)
    if (loop.status != 'running' or loop.current_round_id != round_row.round_id
        or round_row.status != 'settled' or round_row.authority_revision != loop.authority_revision
        or round_row.goal_revision != loop.goal_revision):
        return None
    usage = await session.get(LoopBudgetUsage, loop.loop_id, with_for_update=True, populate_existing=True)
    frontier = await current_frontier_hash(session, loop.loop_id) or round_row.frontier_hash
    progress = await RoundProgressReader().read(session, loop, round_row, frontier)
    if usage is not None:
        usage.no_progress_count = 0 if progress['progressed'] else int(usage.no_progress_count or 0) + 1
        usage.no_progress_fingerprint = progress['fingerprint']
    round_row.barrier = {**(round_row.barrier or {}), 'progress': progress}
    next_round = await create_observation_round(session, loop, round_row, frontier)
    session.add(next_round)
    loop.current_round_id = next_round.round_id
    loop.health = 'observing'
    loop.revision += 1
    await LoopLifecycleEventRecorder().record(session, loop)
    return next_round


async def _terminate_patrol(session, round_row, category, reason) -> None:
    from backend.app.desktop.agent_loop.patrol_session_models import LoopPatrolSession
    from backend.app.desktop.agent_loop.patrol_session_repository import PatrolSessionRepository
    from backend.app.desktop.agent_loop.patrol_session_state import PatrolActivity, PatrolPhase

    patrol = await session.scalar(select(LoopPatrolSession).where(LoopPatrolSession.round_id == round_row.round_id).with_for_update())
    if patrol is None or patrol.status != "active":
        return
    failed_phase = patrol.current_phase
    await PatrolSessionRepository().transition(session, patrol.session_id, PatrolPhase.FAILED,
        PatrolActivity(summary="本轮准备或决策已失败，等待用户处理"),
        terminal_outcome={"status": "failed", "reason_code": category, "reason": reason[:2000],
                          "failed_phase": failed_phase, "observation_id": round_row.observation_id})


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
        frontier_hash=await current_frontier_hash(session, loop.loop_id) or (prior.frontier_hash if prior else fallback_frontier_hash),
        workspace_revision=slot.revision if slot else 1,
    )


async def current_frontier_hash(session: AsyncSession, loop_id: str) -> str | None:
    memberships = tuple((await session.scalars(select(LoopContextMembership).where(
        LoopContextMembership.loop_id == loop_id, LoopContextMembership.status == "active",
    ).order_by(LoopContextMembership.membership_id))).all())
    if not memberships:
        return None
    frontier = []
    for membership in memberships:
        context = await session.get(DesktopThread, membership.context_id)
        frontier.append({"context_id": membership.context_id, "revision_id": context.current_revision_id if context else None})
    payload = json.dumps(frontier, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


async def _settled_decisions(session: AsyncSession, round_ids: list[str]) -> dict[str, str]:
    """只承认生命周期已终止的决策：publishing/adopting 表示专职组件仍在推进，不算落定。"""
    rows = await session.execute(
        select(LoopDecision.round_id, LoopDecision.decision_id).where(
            LoopDecision.round_id.in_(round_ids),
            LoopDecision.status.in_(SETTLED_DECISION_STATUSES),
        )
    )
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
