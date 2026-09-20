r"""本文件对外提供 ProjectionFailureClassifier 与 ProjectionRecoveryRepository。

输入为稳定投影单元身份、异常、重试上限和 journal 边界；输出为幂等累计的 retryable/quarantined/resolved 记录及尝试 outcome。
具体工作流为先分类可重试错误，在独立事务中锁定失败记录并累计 attempt，到达上限后隔离，成功修复时显式 resolve。
示例：`await repository.record_failure(session, loop_id="l1", unit_id="r1", error=exc)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.persistence_safety import PersistencePayloadNormalizer
from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure, LoopProjectionUnitOutcome


class ProjectionFailureClassifier:
    @staticmethod
    def retryable(error: Exception) -> bool:
        return isinstance(error, (OperationalError, TimeoutError, ConnectionError)) or (
            isinstance(error, DBAPIError) and bool(error.connection_invalidated)
        )


class ProjectionRecoveryRepository:
    async def record_failure(
        self,
        session: AsyncSession,
        *,
        loop_id: str,
        projector_name: str,
        unit_kind: str,
        unit_id: str,
        error: Exception,
        boundary_sequence: int = 0,
        max_attempts: int = 3,
    ) -> LoopProjectionFailure:
        row = await session.scalar(
            select(LoopProjectionFailure).where(
                LoopProjectionFailure.loop_id == loop_id,
                LoopProjectionFailure.projector_name == projector_name,
                LoopProjectionFailure.unit_kind == unit_kind,
                LoopProjectionFailure.unit_id == unit_id,
            ).with_for_update()
        )
        retryable = ProjectionFailureClassifier.retryable(error)
        attempt = 1 if row is None else row.attempt_count + 1
        status = "retryable" if retryable and attempt < max_attempts else "quarantined"
        message = PersistencePayloadNormalizer.normalize(str(error), "projection-failure.error").value[:4000]
        if row is None:
            row = LoopProjectionFailure(
                failure_id=uuid.uuid4().hex,
                loop_id=loop_id,
                projector_name=projector_name,
                unit_kind=unit_kind,
                unit_id=unit_id,
                status=status,
                error_class=type(error).__name__,
                error_message=message,
                retryable=retryable,
                attempt_count=attempt,
                boundary_sequence=boundary_sequence,
            )
            session.add(row)
        else:
            row.status = status
            row.error_class = type(error).__name__
            row.error_message = message
            row.retryable = retryable
            row.attempt_count = attempt
            row.boundary_sequence = max(row.boundary_sequence, boundary_sequence)
            row.last_failed_at = datetime.now(UTC)
            row.resolved_at = None
        session.add(self._outcome(row, attempt, status, boundary_sequence))
        await session.flush()
        return row

    async def resolve(
        self,
        session: AsyncSession,
        *,
        loop_id: str,
        projector_name: str,
        unit_kind: str,
        unit_id: str,
        boundary_sequence: int = 0,
    ) -> LoopProjectionFailure | None:
        row = await session.scalar(
            select(LoopProjectionFailure).where(
                LoopProjectionFailure.loop_id == loop_id,
                LoopProjectionFailure.projector_name == projector_name,
                LoopProjectionFailure.unit_kind == unit_kind,
                LoopProjectionFailure.unit_id == unit_id,
            ).with_for_update()
        )
        if row is None:
            attempt = 1
        else:
            row.status = "resolved"
            row.resolved_at = datetime.now(UTC)
            row.boundary_sequence = max(row.boundary_sequence, boundary_sequence)
            attempt = row.attempt_count + 1
        outcome = self._outcome_values(
            loop_id=loop_id,
            projector_name=projector_name,
            unit_kind=unit_kind,
            unit_id=unit_id,
            attempt=attempt,
            status="succeeded",
            boundary_sequence=boundary_sequence,
        )
        await session.execute(
            insert(LoopProjectionUnitOutcome)
            .values(**outcome)
            .on_conflict_do_nothing(
                index_elements=["loop_id", "projector_name", "unit_kind", "unit_id", "attempt"]
            )
        )
        await session.flush()
        return row

    @staticmethod
    def _outcome(
        failure: LoopProjectionFailure,
        attempt: int,
        status: str,
        boundary_sequence: int,
    ) -> LoopProjectionUnitOutcome:
        return LoopProjectionUnitOutcome(**ProjectionRecoveryRepository._outcome_values(
            loop_id=failure.loop_id,
            projector_name=failure.projector_name,
            unit_kind=failure.unit_kind,
            unit_id=failure.unit_id,
            attempt=attempt,
            status=status,
            boundary_sequence=boundary_sequence,
        ))

    @staticmethod
    def _outcome_values(
        *,
        loop_id: str,
        projector_name: str,
        unit_kind: str,
        unit_id: str,
        attempt: int,
        status: str,
        boundary_sequence: int,
    ) -> dict:
        return {
            "outcome_id": uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"focus:projection:{loop_id}:{projector_name}:{unit_kind}:{unit_id}:{attempt}",
            ).hex,
            "loop_id": loop_id,
            "projector_name": projector_name,
            "unit_kind": unit_kind,
            "unit_id": unit_id,
            "attempt": attempt,
            "status": status,
            "boundary_sequence": boundary_sequence,
        }
