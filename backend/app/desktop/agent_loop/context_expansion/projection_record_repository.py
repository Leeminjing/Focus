r"""本文件对外提供 ProjectionRecordRepository。

输入为独立 AsyncSession、Context identity 和有完整验证记录的 segment records；输出为经完整性检查的记录与首个提交赢家。
工作流为按 Context 隔离，DO NOTHING 处理唯一键竞争，下一条 SELECT 读取权威 payload，再校验列与 payload 身份一致。
示例：winner = await repository.put(session, context_id, record)。
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .models import LoopSegmentProjectionRecord
from .segment_projection import SegmentProjectionRecord


class ProjectionRecordRepository:
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
