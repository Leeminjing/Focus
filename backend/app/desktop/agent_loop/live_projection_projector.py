r"""本文件对外提供 LoopLiveSnapshotProjector.project 与 rebuild。

输入为 AsyncSession、Loop id、可选 sequence 边界、已有投影与规范 journal；输出为截止该边界的 LoopLiveProjection。
具体工作流为直接使用调用方边界，或在独立投影时读取一次 journal last_sequence，再分页归约并附加 lag/
rebuild 诊断，不写持久游标；rebuild 从保留历史的起点重复同一 reducer。示例：`snapshot = await projector.project(session, loop_id, boundary=12)`。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.journal_models import LoopJournalSequence
from backend.app.desktop.agent_loop.live_projection_contract import LoopLiveProjection, ProjectionDiagnostics
from backend.app.desktop.agent_loop.live_projection_reducer import LoopLiveProjectionReducer


class LoopLiveSnapshotProjector:
    def __init__(self, journal: LoopEventJournal | None = None, reducer: LoopLiveProjectionReducer | None = None, page_size: int = 200) -> None:
        self._journal = journal or LoopEventJournal()
        self._reducer = reducer or LoopLiveProjectionReducer()
        self._page_size = max(1, min(page_size, 1000))

    async def project(self, session: AsyncSession, loop_id: str, base: LoopLiveProjection | None = None, *, boundary: int | None = None) -> LoopLiveProjection:
        projection = base or LoopLiveProjection(loop_id=loop_id)
        if boundary is None:
            sequence_row = await session.get(LoopJournalSequence, loop_id)
            boundary = int(sequence_row.last_sequence if sequence_row is not None else 0)
        projection = await self._reduce_to(session, projection, boundary)
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
