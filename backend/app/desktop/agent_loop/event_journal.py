r"""本文件对外提供 LoopEventJournal、ReplayUnavailable 与 StaleEntityRevision。

输入为调用方事务中的 AsyncSession、Loop id、CanonicalEventDraft、replay cursor、保留配置和 canonical emission 开关；输出为安全规范化后原子分配的
CanonicalEventEnvelope/显式跳过、严格有序分页或 snapshot-required 错误。具体工作流为开关启用时锁定每 Loop sequence 行，
在同一事务内复查幂等/实体 revision 后追加事件；读取先核对 retention，清理只推进最早可重放边界。
示例：`event = await journal.append(session, loop_id, draft)`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft, CanonicalEventEnvelope, EventVisibility
from backend.app.desktop.agent_loop.feature_flags import LoopFeatureFlags
from backend.app.desktop.agent_loop.journal_models import LoopJournalEvent, LoopJournalSequence, LoopReplayRetention
from backend.app.desktop.persistence_safety import PersistencePayloadNormalizer


class ReplayUnavailable(RuntimeError):
    def __init__(self, minimum_sequence: int) -> None:
        super().__init__(f"cursor 已过期，必须从 snapshot 恢复；minimum_sequence={minimum_sequence}")
        self.minimum_sequence = minimum_sequence


class StaleEntityRevision(RuntimeError):
    pass


class LoopEventJournal:
    def __init__(self, enabled: bool | None = None) -> None:
        self._enabled = LoopFeatureFlags.from_env().canonical_event_emission if enabled is None else enabled

    async def append(
        self,
        session: AsyncSession,
        loop_id: str,
        draft: CanonicalEventDraft,
    ) -> CanonicalEventEnvelope | None:
        if not self._enabled:
            return None
        existing = await self._existing(session, loop_id, draft)
        if existing is not None:
            return self._envelope(existing)
        visibility = PersistencePayloadNormalizer.normalize(
            draft.visibility.model_dump(mode="json"), "loop-journal.visibility"
        )
        payload = PersistencePayloadNormalizer.normalize(draft.payload, "loop-journal.payload")
        safe_payload = payload.value
        if payload.replacement_count and isinstance(safe_payload, dict):
            safe_payload = {**safe_payload, "_persistence_safety": payload.metadata()}
        await session.execute(insert(LoopJournalSequence).values(loop_id=loop_id, last_sequence=0).on_conflict_do_nothing(index_elements=["loop_id"]))
        sequence_row = await session.get(LoopJournalSequence, loop_id, with_for_update=True)
        existing = await self._existing(session, loop_id, draft)
        if existing is not None:
            return self._envelope(existing)
        latest_revision = await session.scalar(
            select(func.max(LoopJournalEvent.entity_revision)).where(
                LoopJournalEvent.loop_id == loop_id,
                LoopJournalEvent.entity_type == draft.entity_type,
                LoopJournalEvent.entity_id == draft.entity_id,
            )
        )
        if latest_revision is not None and int(latest_revision) > draft.entity_revision:
            raise StaleEntityRevision(f"entity revision {draft.entity_revision} 早于已提交 revision {latest_revision}")
        sequence_row.last_sequence += 1
        row = LoopJournalEvent(
            event_id=draft.event_id or uuid.uuid4().hex,
            loop_id=loop_id,
            sequence=sequence_row.last_sequence,
            schema_version=draft.schema_version,
            kind=draft.kind,
            entity_type=draft.entity_type,
            entity_id=draft.entity_id,
            entity_revision=draft.entity_revision,
            correlation_id=draft.correlation_id,
            causation_id=draft.causation_id,
            visibility=visibility.value,
            payload=safe_payload,
            idempotency_key=draft.idempotency_key,
            retained_until=draft.retained_until,
        )
        session.add(row)
        await session.execute(insert(LoopReplayRetention).values(loop_id=loop_id).on_conflict_do_nothing(index_elements=["loop_id"]))
        await session.flush()
        return self._envelope(row)

    async def read(
        self,
        session: AsyncSession,
        loop_id: str,
        after_sequence: int,
        limit: int = 200,
    ) -> tuple[CanonicalEventEnvelope, ...]:
        retention = await session.get(LoopReplayRetention, loop_id)
        minimum = retention.minimum_sequence if retention is not None else 1
        if after_sequence < minimum - 1:
            raise ReplayUnavailable(minimum)
        rows = tuple((await session.scalars(select(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.sequence > after_sequence).order_by(LoopJournalEvent.sequence).limit(max(1, min(limit, 1000))))).all())
        return tuple(self._envelope(row) for row in rows)

    async def configure_retention(self, session: AsyncSession, loop_id: str, retention_seconds: int) -> None:
        await session.execute(insert(LoopReplayRetention).values(loop_id=loop_id, retention_seconds=retention_seconds).on_conflict_do_update(index_elements=["loop_id"], set_={"retention_seconds": retention_seconds}))

    async def prune(self, session: AsyncSession, loop_id: str, now: datetime | None = None) -> int:
        retention = await session.get(LoopReplayRetention, loop_id, with_for_update=True)
        if retention is None:
            return 0
        threshold = (now or datetime.now(UTC)) - timedelta(seconds=retention.retention_seconds)
        retained_minimum = await session.scalar(select(func.min(LoopJournalEvent.sequence)).where(LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.occurred_at >= threshold))
        last_sequence = await session.scalar(select(func.max(LoopJournalEvent.sequence)).where(LoopJournalEvent.loop_id == loop_id))
        minimum = int(retained_minimum or ((last_sequence or 0) + 1))
        result = await session.execute(delete(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.sequence < minimum))
        retention.minimum_sequence = minimum
        return int(result.rowcount or 0)

    @staticmethod
    async def _existing(session: AsyncSession, loop_id: str, draft: CanonicalEventDraft) -> LoopJournalEvent | None:
        row = await session.scalar(select(LoopJournalEvent).where(LoopJournalEvent.loop_id == loop_id, LoopJournalEvent.idempotency_key == draft.idempotency_key))
        if row is not None:
            return row
        return await session.scalar(
            select(LoopJournalEvent).where(
                LoopJournalEvent.loop_id == loop_id,
                LoopJournalEvent.kind == draft.kind,
                LoopJournalEvent.entity_type == draft.entity_type,
                LoopJournalEvent.entity_id == draft.entity_id,
                LoopJournalEvent.entity_revision == draft.entity_revision,
            )
        )

    @staticmethod
    def _envelope(row: LoopJournalEvent) -> CanonicalEventEnvelope:
        return CanonicalEventEnvelope(
            event_id=row.event_id,
            loop_id=row.loop_id,
            sequence=row.sequence,
            schema_version=row.schema_version,
            kind=row.kind,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            entity_revision=row.entity_revision,
            correlation_id=row.correlation_id,
            causation_id=row.causation_id,
            visibility=EventVisibility.model_validate(row.visibility),
            payload=row.payload,
            idempotency_key=row.idempotency_key,
            occurred_at=row.occurred_at,
            retained_until=row.retained_until,
        )
