r"""本文件对外提供 Bootstrap/Lane CuratorScope、CuratorAssignmentRepository 与 CuratorAssignmentRejected。

输入为 Patrol Session、Worker request、稳定 assignment key、受限 scope、安全摘要和 evidence references；输出为
queued/reading/analyzing/proposed/consumed/failed/cancelled 的持久 Curator 当前状态与规范事件。具体工作流为
create 幂等登记 assignment，transition 行锁验证单向状态机并追加 revision，查询按 round 保留部分完成顺序。
示例：`assignment = await repository.create(session, ...)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.patrol_session_models import LoopCuratorAssignment
from backend.app.desktop.agent_loop.patrol_session_state import PatrolEvidenceReference


class CuratorScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["bootstrap", "lane"]
    lane_id: str | None = Field(default=None, max_length=120)
    context_id: str = Field(min_length=1, max_length=120)
    revision_id: str = Field(min_length=1, max_length=120)
    role: str | None = Field(default=None, max_length=120)


class CuratorAssignmentRejected(ValueError):
    pass


class CuratorAssignmentRepository:
    _TERMINAL = frozenset({"consumed", "failed", "cancelled"})
    _EDGES = {
        "queued": frozenset({"reading", "cancelled"}),
        "reading": frozenset({"analyzing", "failed", "cancelled"}),
        "analyzing": frozenset({"analyzing", "proposed", "failed", "cancelled"}),
        "proposed": frozenset({"consumed", "cancelled"}),
    }

    def __init__(self) -> None:
        self._journal = LoopEventJournal()

    async def create(
        self,
        session: AsyncSession,
        *,
        assignment_key: str,
        session_id: str,
        loop_id: str,
        round_id: str,
        worker_request_id: str,
        scope: dict,
    ) -> LoopCuratorAssignment:
        safe_scope = CuratorScope.model_validate(scope).model_dump(mode="json")
        existing = await session.scalar(
            select(LoopCuratorAssignment)
            .where(
                LoopCuratorAssignment.session_id == session_id,
                LoopCuratorAssignment.assignment_key == assignment_key,
            )
            .with_for_update()
        )
        if existing is not None:
            return existing
        row = LoopCuratorAssignment(
            assignment_id=uuid.uuid4().hex,
            assignment_key=assignment_key,
            session_id=session_id,
            loop_id=loop_id,
            round_id=round_id,
            worker_request_id=worker_request_id,
            scope=safe_scope,
        )
        session.add(row)
        await session.flush()
        await self._event(session, row, "curator.assignment.queued", "Curator assignment 已排队")
        return row

    async def transition(
        self,
        session: AsyncSession,
        assignment_id: str,
        target: str,
        summary: str,
        *,
        evidence_refs: tuple[dict, ...] = (),
        result_summary: str | None = None,
        failure: str | None = None,
    ) -> LoopCuratorAssignment:
        row = await session.get(LoopCuratorAssignment, assignment_id, with_for_update=True)
        if row is None:
            raise LookupError("Curator assignment 不存在")
        if row.state in self._TERMINAL or target not in self._EDGES.get(row.state, frozenset()):
            raise CuratorAssignmentRejected(f"非法 Curator transition: {row.state} -> {target}")
        row.state = target
        row.revision += 1
        if evidence_refs:
            row.evidence_refs = [PatrolEvidenceReference.model_validate(item).model_dump(mode="json") for item in evidence_refs]
        if result_summary is not None:
            row.result_summary = result_summary[:1000]
        if failure is not None:
            row.failure = failure[:1000]
        if target in self._TERMINAL:
            row.completed_at = datetime.now(UTC)
        await self._event(session, row, f"curator.assignment.{target}", summary)
        return row

    async def by_worker(self, session: AsyncSession, worker_request_id: str, *, lock: bool = False) -> LoopCuratorAssignment | None:
        query = select(LoopCuratorAssignment).where(LoopCuratorAssignment.worker_request_id == worker_request_id)
        if lock:
            query = query.with_for_update()
        return await session.scalar(query)

    async def by_session(self, session: AsyncSession, session_id: str) -> tuple[LoopCuratorAssignment, ...]:
        return tuple(
            (
                await session.scalars(
                    select(LoopCuratorAssignment)
                    .where(LoopCuratorAssignment.session_id == session_id)
                    .order_by(LoopCuratorAssignment.created_at, LoopCuratorAssignment.assignment_id)
                )
            ).all()
        )

    async def _event(self, session: AsyncSession, row: LoopCuratorAssignment, kind: str, summary: str) -> None:
        await self._journal.append(
            session,
            row.loop_id,
            CanonicalEventDraft(
                kind=kind,
                entity_type="curator",
                entity_id=row.assignment_id,
                entity_revision=row.revision,
                correlation_id=row.session_id,
                payload={
                    "assignment_id": row.assignment_id,
                    "session_id": row.session_id,
                    "round_id": row.round_id,
                    "state": row.state,
                    "summary": summary[:500],
                    "scope": row.scope,
                    "evidence_refs": row.evidence_refs,
                    "result_summary": row.result_summary,
                    "failure": row.failure,
                },
                idempotency_key=f"curator:{row.assignment_id}:revision:{row.revision}",
            ),
        )
