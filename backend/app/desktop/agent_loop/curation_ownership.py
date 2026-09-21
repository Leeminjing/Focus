r"""本文件对外提供 CurationOwnershipRepository、CurationOwnershipRecovery、CurationOwnership 与 CurationOwnershipConflict。

输入为数据库事务、Context 或终态 Loop identity、Lane/Program 持久关系及释放原因；输出为锁定后的单发布者所有权状态、幂等释放结果或结构化冲突。
具体工作流为先锁 Context 与非 retired Lane，再由 Program 反查唯一 owner Loop；终态 owner 可原位退休 Lane，非终态或关系不一致则失败关闭，并用规范事件记录获取、释放和修复。
示例：`prior = await repository.prepare_successor(session, context_id, reason="successor_authorization")`。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent
from backend.app.desktop.context_curation.models import CurationLane, CurationProgram
from backend.app.desktop.models import DesktopThread


TERMINAL_LOOP_STATUSES = frozenset({"completed", "stopped", "failed"})


class CurationOwnershipState(StrEnum):
    FREE = "free"
    TERMINAL_STALE = "terminal_stale"
    OWNED = "owned"
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True, slots=True)
class CurationOwnership:
    state: CurationOwnershipState
    context_id: str
    lane_id: str | None = None
    program_id: str | None = None
    owner_loop_id: str | None = None
    owner_status: str | None = None
    lane_lifecycle: str | None = None
    reason: str | None = None

    def payload(self) -> dict[str, str | None]:
        return {**asdict(self), "state": self.state.value}


class CurationOwnershipConflict(RuntimeError):
    def __init__(self, ownership: CurationOwnership) -> None:
        self.ownership = ownership
        super().__init__(ownership.reason or "Context 策展所有权冲突")

    def detail(self) -> dict[str, str | None]:
        return {
            "code": "curation_ownership_conflict",
            "message": str(self),
            **self.ownership.payload(),
        }


@dataclass(frozen=True, slots=True)
class CurationOwnershipRecoveryReport:
    repaired_loop_ids: tuple[str, ...]
    retired_lane_ids: tuple[str, ...]
    diagnostics: tuple[CurationOwnership, ...]


class CurationOwnershipRepository:
    def __init__(self, journal: LoopEventJournal | None = None) -> None:
        self._journal = journal or LoopEventJournal()

    async def lock_context(self, session: AsyncSession, context_id: str) -> DesktopThread | None:
        return await session.scalar(
            select(DesktopThread).where(DesktopThread.task_id == context_id).with_for_update()
        )

    async def live_owner(self, session: AsyncSession, context_id: str) -> CurationOwnership:
        lanes = tuple(
            (
                await session.scalars(
                    select(CurationLane)
                    .where(
                        CurationLane.managed_context_id == context_id,
                        CurationLane.lifecycle != "retired",
                    )
                    .with_for_update()
                )
            ).all()
        )
        if not lanes:
            return CurationOwnership(CurationOwnershipState.FREE, context_id)
        if len(lanes) != 1:
            return self._inconsistent(context_id, lanes[0], "Context 存在多个非 retired 策展 Lane")
        lane = lanes[0]
        program = await session.get(CurationProgram, lane.program_id, with_for_update=True)
        if program is None:
            return self._inconsistent(context_id, lane, "Lane 对应的 Curation Program 不存在")
        owners = tuple(
            (
                await session.scalars(
                    select(AgentLoop).where(AgentLoop.program_id == program.program_id).with_for_update()
                )
            ).all()
        )
        if len(owners) != 1:
            reason = "Curation Program 没有 owner Loop" if not owners else "Curation Program 对应多个 owner Loop"
            return self._inconsistent(context_id, lane, reason, program_id=program.program_id)
        owner = owners[0]
        state = CurationOwnershipState.TERMINAL_STALE if owner.status in TERMINAL_LOOP_STATUSES else CurationOwnershipState.OWNED
        return CurationOwnership(
            state=state,
            context_id=context_id,
            lane_id=lane.lane_id,
            program_id=program.program_id,
            owner_loop_id=owner.loop_id,
            owner_status=owner.status,
            lane_lifecycle=lane.lifecycle,
            reason="终态 Loop 仍持有策展发布权" if state == CurationOwnershipState.TERMINAL_STALE else "非终态 Loop 正在持有策展发布权",
        )

    async def prepare_successor(
        self,
        session: AsyncSession,
        context_id: str,
        *,
        reason: str,
    ) -> CurationOwnership:
        context = await self.lock_context(session, context_id)
        if context is None:
            raise CurationOwnershipConflict(
                CurationOwnership(CurationOwnershipState.INCONSISTENT, context_id, reason="Context 不存在")
            )
        ownership = await self.live_owner(session, context_id)
        if ownership.state == CurationOwnershipState.FREE:
            return ownership
        if ownership.state != CurationOwnershipState.TERMINAL_STALE:
            await self._record_rejection(session, ownership, reason)
            raise CurationOwnershipConflict(ownership)
        owner = await session.get(AgentLoop, ownership.owner_loop_id, with_for_update=True)
        if owner is None or owner.status not in TERMINAL_LOOP_STATUSES or owner.program_id != ownership.program_id:
            conflict = self._inconsistent(
                context_id,
                await session.get(CurationLane, ownership.lane_id),
                "终态 predecessor 所有权证明在释放前发生变化",
                program_id=ownership.program_id,
                owner_loop_id=ownership.owner_loop_id,
                owner_status=owner.status if owner is not None else None,
            )
            await self._record_rejection(session, conflict, reason)
            raise CurationOwnershipConflict(conflict)
        await self.release_terminal(session, owner, reason=reason, event_kind="curation.ownership.repaired")
        return ownership

    async def release_terminal(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        *,
        reason: str,
        event_kind: str = "curation.ownership.released",
    ) -> tuple[str, ...]:
        if loop.status not in TERMINAL_LOOP_STATUSES:
            raise ValueError(f"只有终态 Loop 可以释放策展所有权: {loop.status}")
        if not loop.program_id:
            return ()
        program = await session.get(CurationProgram, loop.program_id, with_for_update=True)
        if program is None:
            raise CurationOwnershipConflict(
                CurationOwnership(
                    CurationOwnershipState.INCONSISTENT,
                    loop.initial_context_id,
                    program_id=loop.program_id,
                    owner_loop_id=loop.loop_id,
                    owner_status=loop.status,
                    reason="终态 Loop 对应的 Curation Program 不存在",
                )
            )
        lanes = tuple(
            (
                await session.scalars(
                    select(CurationLane)
                    .where(
                        CurationLane.program_id == program.program_id,
                        CurationLane.managed_context_id.is_not(None),
                        CurationLane.lifecycle != "retired",
                    )
                    .with_for_update()
                )
            ).all()
        )
        if program.control_state != "stopped":
            program.control_state = "stopped"
            program.revision += 1
        for lane in lanes:
            lane.lifecycle = "retired"
            lane.publisher_epoch += 1
            await self._record_release(session, loop, lane, reason, event_kind)
        return tuple(lane.lane_id for lane in lanes)

    async def record_acquisition(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        lane: CurationLane,
        *,
        predecessor: CurationOwnership | None,
        reason: str,
    ) -> None:
        await self._journal.append(
            session,
            loop.loop_id,
            CanonicalEventDraft(
                kind="curation.ownership.acquired",
                entity_type="curation_lane",
                entity_id=lane.lane_id,
                entity_revision=lane.publisher_epoch,
                correlation_id=loop.loop_id,
                payload={
                    "loop_id": loop.loop_id,
                    "context_id": lane.managed_context_id,
                    "program_id": lane.program_id,
                    "lane_id": lane.lane_id,
                    "reason": reason,
                    "predecessor": predecessor.payload() if predecessor and predecessor.lane_id else None,
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
                idempotency_key=f"curation-ownership-acquired:{loop.loop_id}:{lane.lane_id}:{lane.publisher_epoch}",
            ),
        )

    async def ensure_repair_audit(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        lane: CurationLane,
        *,
        reason: str,
    ) -> bool:
        existing = await session.scalar(
            select(LoopJournalEvent.event_id).where(
                LoopJournalEvent.loop_id == loop.loop_id,
                LoopJournalEvent.entity_type == "curation_lane",
                LoopJournalEvent.entity_id == lane.lane_id,
                LoopJournalEvent.kind.in_(("curation.ownership.released", "curation.ownership.repaired")),
            ).limit(1)
        )
        if existing is not None:
            return False
        await self._record_release(session, loop, lane, reason, "curation.ownership.repaired")
        return True

    async def _record_release(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        lane: CurationLane,
        reason: str,
        event_kind: str,
    ) -> None:
        await self._journal.append(
            session,
            loop.loop_id,
            CanonicalEventDraft(
                kind=event_kind,
                entity_type="curation_lane",
                entity_id=lane.lane_id,
                entity_revision=lane.publisher_epoch,
                correlation_id=loop.loop_id,
                payload={
                    "loop_id": loop.loop_id,
                    "loop_status": loop.status,
                    "context_id": lane.managed_context_id,
                    "program_id": lane.program_id,
                    "lane_id": lane.lane_id,
                    "lifecycle": lane.lifecycle,
                    "reason": reason,
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
                idempotency_key=f"{event_kind}:{loop.loop_id}:{lane.lane_id}:{lane.publisher_epoch}",
            ),
        )

    async def _record_rejection(
        self,
        session: AsyncSession,
        ownership: CurationOwnership,
        reason: str,
    ) -> None:
        if not ownership.owner_loop_id:
            return
        await self._journal.append(
            session,
            ownership.owner_loop_id,
            CanonicalEventDraft(
                kind="curation.ownership.rejected",
                entity_type="curation_lane",
                entity_id=ownership.lane_id or ownership.context_id,
                entity_revision=1,
                correlation_id=ownership.owner_loop_id,
                payload={**ownership.payload(), "recovery_reason": reason, "occurred_at": datetime.now(UTC).isoformat()},
                idempotency_key=f"curation-ownership-rejected:{ownership.owner_loop_id}:{ownership.context_id}:{reason}",
            ),
        )

    @staticmethod
    def _inconsistent(
        context_id: str,
        lane: CurationLane | None,
        reason: str,
        *,
        program_id: str | None = None,
        owner_loop_id: str | None = None,
        owner_status: str | None = None,
    ) -> CurationOwnership:
        return CurationOwnership(
            CurationOwnershipState.INCONSISTENT,
            context_id,
            lane_id=lane.lane_id if lane is not None else None,
            program_id=program_id or (lane.program_id if lane is not None else None),
            owner_loop_id=owner_loop_id,
            owner_status=owner_status,
            lane_lifecycle=lane.lifecycle if lane is not None else None,
            reason=reason,
        )


class CurationOwnershipRecovery:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        ownership: CurationOwnershipRepository | None = None,
    ) -> None:
        self._sessions = sessions
        self._ownership = ownership or CurationOwnershipRepository()

    async def reconcile(self) -> CurationOwnershipRecoveryReport:
        async with self._sessions() as session:
            loop_ids = tuple(
                (
                    await session.scalars(
                        select(AgentLoop.loop_id)
                        .join(CurationLane, CurationLane.program_id == AgentLoop.program_id)
                        .where(AgentLoop.status.in_(TERMINAL_LOOP_STATUSES), CurationLane.managed_context_id.is_not(None))
                        .distinct()
                    )
                ).all()
            )
        repaired: list[str] = []
        retired: list[str] = []
        for loop_id in loop_ids:
            async with self._sessions.begin() as session:
                loop = await session.get(AgentLoop, loop_id, with_for_update=True)
                if loop is None or loop.status not in TERMINAL_LOOP_STATUSES:
                    continue
                released = await self._ownership.release_terminal(
                    session,
                    loop,
                    reason="startup_reconciliation",
                    event_kind="curation.ownership.repaired",
                )
                if released:
                    repaired.append(loop_id)
                    retired.extend(released)
                lanes = tuple(
                    (
                        await session.scalars(
                            select(CurationLane).where(
                                CurationLane.program_id == loop.program_id,
                                CurationLane.managed_context_id.is_not(None),
                                CurationLane.lifecycle == "retired",
                            )
                        )
                    ).all()
                )
                for lane in lanes:
                    await self._ownership.ensure_repair_audit(
                        session,
                        loop,
                        lane,
                        reason="startup_reconciliation",
                    )
        diagnostics = await self._diagnostics()
        return CurationOwnershipRecoveryReport(tuple(repaired), tuple(retired), diagnostics)

    async def _diagnostics(self) -> tuple[CurationOwnership, ...]:
        async with self._sessions.begin() as session:
            context_ids = tuple(
                (
                    await session.scalars(
                        select(CurationLane.managed_context_id)
                        .where(
                            CurationLane.managed_context_id.is_not(None),
                            CurationLane.lifecycle != "retired",
                        )
                        .distinct()
                    )
                ).all()
            )
            states = [await self._ownership.live_owner(session, context_id) for context_id in context_ids]
        return tuple(state for state in states if state.state == CurationOwnershipState.INCONSISTENT)
