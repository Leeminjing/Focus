r"""本文件对外提供 CurationProgramRepository、PortfolioRepository 与陈旧写入错误。

输入为 Program、订阅、Lane、冻结 Portfolio、candidate 和计算/发布 attempt 的持久化参数；输出为
已 flush 的 ORM 实体或 CAS 结果。具体工作流为用短方法创建聚合记录，完整保存控制/Lane 快照，
以 publisher epoch 和 Program revision 保护写入，再由数据库唯一约束裁决并发单发布者。示例：
`lane = await programs.add_lane(session, program_id, "Testing", managed_context_id=cid)`。
"""

from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.context_curation.models import (
    CurationAttempt,
    CurationAttemptStatus,
    CurationLane,
    CurationLaneLifecycle,
    CurationProgram,
    CurationProgramControlState,
    CurationSourceSubscription,
    CurationWorkerKind,
    PortfolioLaneAction,
    PortfolioLaneCandidate,
    PortfolioLaneCandidateStatus,
    PortfolioPublicationAttempt,
    PortfolioPublicationAttemptStatus,
    PortfolioRevision,
    PortfolioRevisionStatus,
)


class CurationPersistenceError(RuntimeError):
    pass


class CurationRecordNotFound(CurationPersistenceError):
    pass


class StaleCurationWrite(CurationPersistenceError):
    pass


class CurationProgramRepository:
    async def create(
        self,
        session: AsyncSession,
        workspace_id: str,
        *,
        patrol_id: str | None = None,
        policy: dict[str, Any] | None = None,
        program_id: str | None = None,
    ) -> CurationProgram:
        program = CurationProgram(
            program_id=program_id or self._id(),
            workspace_id=workspace_id,
            patrol_id=patrol_id,
            control_state=CurationProgramControlState.FOLLOWING.value,
            policy=policy or {},
            revision=0,
        )
        session.add(program)
        await session.flush()
        return program

    async def subscribe(
        self,
        session: AsyncSession,
        program_id: str,
        source_context_id: str,
        source_role: str,
        position: int,
        *,
        selection_policy: dict[str, Any] | None = None,
        subscription_id: str | None = None,
    ) -> CurationSourceSubscription:
        subscription = CurationSourceSubscription(
            subscription_id=subscription_id or self._id(),
            program_id=program_id,
            source_context_id=source_context_id,
            source_role=source_role,
            selection_policy=selection_policy or {},
            position=position,
        )
        session.add(subscription)
        await session.flush()
        return subscription

    async def add_lane(
        self,
        session: AsyncSession,
        program_id: str,
        purpose: str,
        *,
        managed_context_id: str | None = None,
        lane_policy: dict[str, Any] | None = None,
        current_source_frontier_hash: str | None = None,
        current_semantic_fingerprint: str | None = None,
        lane_id: str | None = None,
    ) -> CurationLane:
        lane = CurationLane(
            lane_id=lane_id or self._id(),
            program_id=program_id,
            managed_context_id=managed_context_id,
            purpose=purpose.strip(),
            normalized_purpose=self.normalize_purpose(purpose),
            lane_policy=lane_policy or {},
            lifecycle=CurationLaneLifecycle.ACTIVE.value,
            publisher_epoch=1,
            current_source_frontier_hash=current_source_frontier_hash,
            current_semantic_fingerprint=current_semantic_fingerprint,
        )
        session.add(lane)
        await session.flush()
        return lane

    async def advance_publisher_epoch(
        self,
        session: AsyncSession,
        lane_id: str,
        expected_epoch: int,
    ) -> CurationLane:
        result = await session.execute(
            update(CurationLane)
            .where(
                CurationLane.lane_id == lane_id,
                CurationLane.publisher_epoch == expected_epoch,
                CurationLane.lifecycle != CurationLaneLifecycle.RETIRED.value,
            )
            .values(publisher_epoch=expected_epoch + 1)
        )
        if result.rowcount != 1:
            raise StaleCurationWrite(f"Lane publisher epoch 已变化: {lane_id}")
        await session.flush()
        lane = await session.get(CurationLane, lane_id)
        if lane is None:
            raise CurationRecordNotFound(f"Lane 不存在: {lane_id}")
        return lane

    @staticmethod
    def normalize_purpose(purpose: str) -> str:
        normalized = " ".join(purpose.casefold().split())
        if not normalized:
            raise ValueError("Lane purpose 不能为空")
        return normalized[:160]

    @staticmethod
    def _id() -> str:
        return uuid.uuid4().hex


class PortfolioRepository:
    async def create_revision(
        self,
        session: AsyncSession,
        program_id: str,
        generation: int,
        source_frontier: list[dict[str, Any]],
        frontier_hash: str,
        base_program_revision: int,
        *,
        status: PortfolioRevisionStatus = PortfolioRevisionStatus.OBSERVED,
        control_revisions: dict[str, Any] | None = None,
        target_lanes: list[dict[str, Any]] | None = None,
        portfolio_revision_id: str | None = None,
    ) -> PortfolioRevision:
        revision = PortfolioRevision(
            portfolio_revision_id=portfolio_revision_id or self._id(),
            program_id=program_id,
            generation=generation,
            source_frontier=source_frontier,
            frontier_hash=frontier_hash,
            base_program_revision=base_program_revision,
            control_revisions=control_revisions or {},
            target_lanes=target_lanes or [],
            status=status.value,
        )
        session.add(revision)
        await session.flush()
        return revision

    async def add_candidate(
        self,
        session: AsyncSession,
        portfolio_revision_id: str,
        lane_id: str,
        action: PortfolioLaneAction,
        purpose: str,
        *,
        base_context_revision_id: str | None = None,
        candidate_context_revision_id: str | None = None,
        target_context_id: str | None = None,
        base_publisher_epoch: int = 1,
        source_allocation: list[dict[str, Any]] | None = None,
        source_frontier_hash: str | None = None,
        semantic_fingerprint: str | None = None,
        message_lineage: list[dict[str, Any]] | None = None,
        source_dispositions: list[dict[str, Any]] | None = None,
        status: PortfolioLaneCandidateStatus = PortfolioLaneCandidateStatus.PENDING,
        candidate_id: str | None = None,
    ) -> PortfolioLaneCandidate:
        candidate = PortfolioLaneCandidate(
            candidate_id=candidate_id or self._id(),
            portfolio_revision_id=portfolio_revision_id,
            lane_id=lane_id,
            action=action.value,
            target_context_id=target_context_id,
            base_publisher_epoch=base_publisher_epoch,
            base_context_revision_id=base_context_revision_id,
            candidate_context_revision_id=candidate_context_revision_id,
            purpose=purpose,
            source_allocation=source_allocation or [],
            source_frontier_hash=source_frontier_hash,
            semantic_fingerprint=semantic_fingerprint,
            message_lineage=message_lineage or [],
            source_dispositions=source_dispositions or [],
            status=status.value,
        )
        session.add(candidate)
        await session.flush()
        return candidate

    async def add_attempt(
        self,
        session: AsyncSession,
        candidate_id: str,
        attempt_number: int,
        worker_kind: CurationWorkerKind,
        model_name: str,
        input_payload: dict[str, Any],
        *,
        status: CurationAttemptStatus = CurationAttemptStatus.PENDING,
        attempt_id: str | None = None,
    ) -> CurationAttempt:
        attempt = CurationAttempt(
            attempt_id=attempt_id or self._id(),
            candidate_id=candidate_id,
            attempt_number=attempt_number,
            worker_kind=worker_kind.value,
            model_name=model_name,
            input_payload=input_payload,
            status=status.value,
        )
        session.add(attempt)
        await session.flush()
        return attempt

    async def add_publication_attempt(
        self,
        session: AsyncSession,
        portfolio_revision_id: str,
        attempt_number: int,
        status: PortfolioPublicationAttemptStatus,
        phase: str,
        *,
        evidence: dict[str, Any] | None = None,
        error: str | None = None,
        attempt_id: str | None = None,
    ) -> PortfolioPublicationAttempt:
        attempt = PortfolioPublicationAttempt(
            attempt_id=attempt_id or self._id(),
            portfolio_revision_id=portfolio_revision_id,
            attempt_number=attempt_number,
            status=status.value,
            phase=phase,
            evidence=evidence or {},
            error=error,
        )
        session.add(attempt)
        await session.flush()
        return attempt

    async def switch_current(
        self,
        session: AsyncSession,
        program_id: str,
        portfolio_revision_id: str,
        *,
        expected_current_id: str | None,
        expected_program_revision: int,
    ) -> CurationProgram:
        current_predicate = (
            CurationProgram.current_portfolio_revision_id.is_(None)
            if expected_current_id is None
            else CurationProgram.current_portfolio_revision_id == expected_current_id
        )
        result = await session.execute(
            update(CurationProgram)
            .where(
                CurationProgram.program_id == program_id,
                CurationProgram.revision == expected_program_revision,
                current_predicate,
            )
            .values(
                current_portfolio_revision_id=portfolio_revision_id,
                revision=expected_program_revision + 1,
            )
        )
        if result.rowcount != 1:
            raise StaleCurationWrite(f"Program current Portfolio 已变化: {program_id}")
        await session.flush()
        program = await session.scalar(
            select(CurationProgram).where(CurationProgram.program_id == program_id)
        )
        if program is None:
            raise CurationRecordNotFound(f"Curation Program 不存在: {program_id}")
        return program

    @staticmethod
    def _id() -> str:
        return uuid.uuid4().hex
