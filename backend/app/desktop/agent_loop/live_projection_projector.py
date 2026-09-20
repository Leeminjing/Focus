r"""本文件对外提供 LoopLiveSnapshotProjector。

输入为 AsyncSession、Loop id、可选已有投影与规范 journal；输出为截止单一已提交 sequence 的 LoopLiveProjection。
具体工作流为先冻结 journal last_sequence，分页归约不超过该边界的事件，再推进 projector cursor 并附加 lag/
rebuild 诊断；rebuild 从保留历史的起点重复同一 reducer。示例：`snapshot = await projector.project(session, loop_id)`。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.journal_models import LoopJournalSequence, LoopProjectorCursor
from backend.app.desktop.agent_loop.live_projection_contract import LoopLiveProjection, ProjectionDiagnostics
from backend.app.desktop.agent_loop.live_projection_reducer import LoopLiveProjectionReducer


class LoopLiveSnapshotProjector:
    def __init__(self, journal: LoopEventJournal | None = None, reducer: LoopLiveProjectionReducer | None = None, page_size: int = 200) -> None:
        self._journal = journal or LoopEventJournal()
        self._reducer = reducer or LoopLiveProjectionReducer()
        self._page_size = max(1, min(page_size, 1000))

    async def project(self, session: AsyncSession, loop_id: str, base: LoopLiveProjection | None = None) -> LoopLiveProjection:
        projection = base or LoopLiveProjection(loop_id=loop_id)
        sequence_row = await session.get(LoopJournalSequence, loop_id)
        boundary = int(sequence_row.last_sequence if sequence_row is not None else 0)
        projection = await self._reduce_to(session, projection, boundary)
        await self._record_cursor(session, loop_id, projection.last_sequence)
        diagnostics = ProjectionDiagnostics(journal_last_sequence=boundary, projector_last_sequence=projection.last_sequence, lag=max(0, boundary - projection.last_sequence), rebuilt=base is None, updated_at=datetime.now(UTC))
        return projection.model_copy(update={"diagnostics": diagnostics})

    async def rebuild(self, session: AsyncSession, loop_id: str) -> LoopLiveProjection:
        return await self.project(session, loop_id, None)

    async def _reduce_to(self, session: AsyncSession, projection: LoopLiveProjection, boundary: int) -> LoopLiveProjection:
        while projection.last_sequence < boundary:
            page = await self._journal.read(session, projection.loop_id, projection.last_sequence, self._page_size)
            page = tuple(event for event in page if event.sequence <= boundary)
            if not page:
                break
            for event in page:
                projection = self._reducer.reduce(projection, event)
        return projection

    @staticmethod
    async def _record_cursor(session: AsyncSession, loop_id: str, last_sequence: int) -> None:
        await session.execute(
            insert(LoopProjectorCursor)
            .values(loop_id=loop_id, projector_name="live_snapshot", last_sequence=last_sequence)
            .on_conflict_do_update(
                index_elements=["loop_id", "projector_name"],
                set_={"last_sequence": last_sequence, "updated_at": datetime.now(UTC)},
            )
        )
