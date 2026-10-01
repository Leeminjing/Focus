r"""本文件对外提供 ProjectionRecordRepository。

输入为独立 AsyncSession、Context identity 和局部验证 records；输出为经完整性检查的记录与首个提交赢家。
工作流为按 Context 隔离，DO NOTHING 处理唯一键竞争，随后 SELECT 权威 payload 并校验列与内容身份。
resolve_existing 在综合前批量采用已有局部赢家；by_ids 依库存顺序返回，put 的竞争赢家用于原子发布。
示例：records = await repository.resolve_existing(session, context_id, records)；综合读取这些权威局部线索。
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .models import LoopSegmentProjectionRecord
from .segment_projection import SegmentProjectionRecord


class ProjectionRecordRepository:
    async def resolve_existing(self, session, context_id, records):
        rows = (
            await session.scalars(
                select(LoopSegmentProjectionRecord).where(
                    LoopSegmentProjectionRecord.context_id == context_id,
                    LoopSegmentProjectionRecord.cache_key.in_(
                        [r.cache_key for r in records]
                    ),
                )
            )
        ).all()
        by_key = {row.cache_key: self._validated(row) for row in rows}
        resolved = []
        for record in records:
            winner = by_key.get(record.cache_key, record)
            winner.validate_target(
                record.segment, record.messages, record.contract_fingerprint
            )
            resolved.append(winner)
        return tuple(resolved)

    async def by_ids(
        self, session, context_id: str, record_ids: tuple[str, ...]
    ) -> tuple[SegmentProjectionRecord, ...] | None:
        if not record_ids:
            return ()
        rows = (
            await session.scalars(
                select(LoopSegmentProjectionRecord).where(
                    LoopSegmentProjectionRecord.context_id == context_id,
                    LoopSegmentProjectionRecord.record_id.in_(record_ids),
                )
            )
        ).all()
        by_id = {row.record_id: self._validated(row) for row in rows}
        if set(by_id) != set(record_ids):
            return None
        return tuple(by_id[key] for key in record_ids)

    async def put(
        self, session, context_id: str, record: SegmentProjectionRecord
    ) -> SegmentProjectionRecord:
        if record.context_id != context_id:
            raise ValueError("segment record context scope mismatch")
        await session.execute(
            insert(LoopSegmentProjectionRecord)
            .values(
                record_id=record.record_id,
                context_id=context_id,
                cache_key=record.cache_key,
                payload=record.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(constraint="uq_loop_segment_projection_key")
        )
        row = await session.scalar(
            select(LoopSegmentProjectionRecord).where(
                LoopSegmentProjectionRecord.context_id == context_id,
                LoopSegmentProjectionRecord.cache_key == record.cache_key,
            )
        )
        if row is None:
            raise ValueError("segment projection committed winner disappeared")
        return self._validated(row)

    @staticmethod
    def _validated(row) -> SegmentProjectionRecord:
        record = SegmentProjectionRecord.model_validate(row.payload)
        if (
            row.context_id != record.context_id
            or row.record_id != record.record_id
            or row.cache_key != record.cache_key
        ):
            raise ValueError("segment projection columns integrity mismatch")
        return record
