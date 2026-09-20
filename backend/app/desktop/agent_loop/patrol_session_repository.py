r"""本文件对外提供 PatrolSessionRepository。

输入为调用方事务、Loop/round/fencing identity、合法 phase 与 PatrolActivity；输出为持久 Session 当前状态、
不可变 phase history 和同事务规范事件。具体工作流为 begin 幂等创建 session，transition 行锁验证状态机并追加
revision/event，history 从结构化记录还原已结束轮次。示例：`session = await repository.begin(db, claim)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.patrol_session_models import LoopPatrolPhaseTransition, LoopPatrolSession
from backend.app.desktop.agent_loop.patrol_session_state import PatrolActivity, PatrolPhase, PatrolSessionStateMachine


class PatrolSessionRepository:
    def __init__(self) -> None:
        self._machine = PatrolSessionStateMachine()
        self._journal = LoopEventJournal()

    async def begin(self, session: AsyncSession, loop_id: str, round_id: str, fencing_token: int) -> LoopPatrolSession:
        existing = await session.scalar(
            select(LoopPatrolSession).where(LoopPatrolSession.round_id == round_id).with_for_update()
        )
        if existing is not None:
            if existing.status == "active" and fencing_token > existing.fencing_token:
                existing.fencing_token = fencing_token
            return existing
        row = LoopPatrolSession(
            session_id=uuid.uuid4().hex,
            loop_id=loop_id,
            round_id=round_id,
            fencing_token=fencing_token,
            safe_summary="Patrol session 已创建",
        )
        session.add(row)
        await session.flush()
        session.add(
            LoopPatrolPhaseTransition(
                transition_id=uuid.uuid4().hex,
                session_id=row.session_id,
                loop_id=loop_id,
                round_id=round_id,
                revision=1,
                from_phase=PatrolPhase.CREATED,
                to_phase=PatrolPhase.CREATED,
                safe_summary=row.safe_summary,
            )
        )
        await self._event(session, row, "patrol.session.created")
        return row

    async def transition(
        self,
        session: AsyncSession,
        session_id: str,
        target: PatrolPhase | str,
        activity: PatrolActivity,
        *,
        observation_id: str | None = None,
        observation_sequence: int | None = None,
        terminal_outcome: dict | None = None,
    ) -> LoopPatrolSession:
        row = await session.get(LoopPatrolSession, session_id, with_for_update=True)
        if row is None:
            raise LookupError("Patrol session 不存在")
        target_phase = self._machine.validate(row.current_phase, str(target))
        previous = row.current_phase
        row.revision += 1
        row.current_phase = target_phase.value
        row.safe_summary = activity.summary
        row.wait_reason = activity.wait_reason
        row.wait_targets = [item.model_dump(mode="json") for item in activity.wait_targets]
        if observation_id is not None:
            row.observation_id = observation_id
        if observation_sequence is not None:
            row.observation_sequence = observation_sequence
        if self._machine.is_terminal(target_phase):
            row.status = target_phase.value
            row.terminal_outcome = terminal_outcome or {"status": target_phase.value}
            row.completed_at = datetime.now(UTC)
        session.add(
            LoopPatrolPhaseTransition(
                transition_id=uuid.uuid4().hex,
                session_id=row.session_id,
                loop_id=row.loop_id,
                round_id=row.round_id,
                revision=row.revision,
                from_phase=previous,
                to_phase=target_phase.value,
                safe_summary=activity.summary,
                wait_reason=activity.wait_reason,
                wait_targets=row.wait_targets,
                evidence_refs=[item.model_dump(mode="json") for item in activity.evidence_refs],
            )
        )
        kind = f"patrol.session.{target_phase.value}" if self._machine.is_terminal(target_phase) else "patrol.session.phase_changed"
        await self._event(session, row, kind)
        return row

    async def history(self, session: AsyncSession, loop_id: str, round_id: str | None = None) -> tuple[dict, ...]:
        query = select(LoopPatrolPhaseTransition).where(LoopPatrolPhaseTransition.loop_id == loop_id)
        if round_id is not None:
            query = query.where(LoopPatrolPhaseTransition.round_id == round_id)
        rows = tuple((await session.scalars(query.order_by(LoopPatrolPhaseTransition.occurred_at, LoopPatrolPhaseTransition.revision))).all())
        return tuple(
            {
                "session_id": row.session_id,
                "round_id": row.round_id,
                "revision": row.revision,
                "from_phase": row.from_phase,
                "to_phase": row.to_phase,
                "summary": row.safe_summary,
                "wait_reason": row.wait_reason,
                "wait_targets": row.wait_targets,
                "evidence_refs": row.evidence_refs,
                "occurred_at": row.occurred_at.isoformat(),
            }
            for row in rows
        )

    async def _event(self, session: AsyncSession, row: LoopPatrolSession, kind: str) -> None:
        await self._journal.append(
            session,
            row.loop_id,
            CanonicalEventDraft(
                kind=kind,
                entity_type="patrol_session",
                entity_id=row.session_id,
                entity_revision=row.revision,
                correlation_id=row.round_id,
                payload={
                    "session_id": row.session_id,
                    "round_id": row.round_id,
                    "status": row.status,
                    "phase": row.current_phase,
                    "summary": row.safe_summary,
                    "wait_reason": row.wait_reason,
                    "wait_targets": row.wait_targets,
                    "observation_id": row.observation_id,
                    "observation_sequence": row.observation_sequence,
                    "terminal_outcome": row.terminal_outcome,
                },
                idempotency_key=f"patrol-session:{row.session_id}:revision:{row.revision}",
            ),
        )
