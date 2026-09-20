r"""本文件对外提供 LoopLiveSnapshotService 与 LoopLiveEventFeed。

输入为 Loop identity、journal cursor、当前授权和物化领域实体；输出为单 sequence 边界完整 snapshot、已脱敏且 sequence 连续的有序事件批次、
snapshot-required 或 resync-required 协议结果。具体工作流为 snapshot 锁定 per-Loop sequence 后组合数据库 current state 与
journal timeline；Loop/current Context state 同时携带渲染所需的授权、预算和 membership 字段；feed 每批复查授权、限制 backlog/page，
未授权事件转换为无敏感 identity 的同 sequence 占位信封，避免客户端 cursor 卡住或误判 gap。
示例：`batch = await feed.read_batch(loop_id, 12)`。
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.event_contract import LiveEventAuthorizationPolicy
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal, ReplayUnavailable
from backend.app.desktop.agent_loop.journal_models import LoopJournalSequence
from backend.app.desktop.agent_loop.live_access import LoopLiveAccessPolicy, LoopLiveRedactionPolicy
from backend.app.desktop.agent_loop.live_projection_projector import LoopLiveSnapshotProjector
from backend.app.desktop.agent_loop.live_snapshot_overlay import LoopLiveProjectionOverlay


class LoopLiveSnapshotService:
    def __init__(self, projector: LoopLiveSnapshotProjector | None = None) -> None:
        self._projector = projector or LoopLiveSnapshotProjector()
        self._access = LoopLiveAccessPolicy()
        self._overlay = LoopLiveProjectionOverlay()

    async def read(self, session: AsyncSession, loop_id: str) -> dict:
        sequence = await session.get(LoopJournalSequence, loop_id, with_for_update=True)
        if sequence is None:
            raise HTTPException(404, "Loop 尚无 Live journal")
        boundary = int(sequence.last_sequence)
        access = await self._access.resolve(session, loop_id)
        projected = await self._projector.project(session, loop_id)
        projection = await self._overlay.apply(session, projected, boundary)
        return LoopLiveRedactionPolicy.apply(projection, access.permissions).model_dump(mode="json")


class LoopLiveEventFeed:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], page_size: int = 200, max_backlog: int = 2000) -> None:
        self._sessions = sessions
        self._journal = LoopEventJournal()
        self._events = LiveEventAuthorizationPolicy()
        self._access = LoopLiveAccessPolicy()
        self._page_size = max(1, min(page_size, 500))
        self._max_backlog = max(self._page_size, max_backlog)

    async def read_batch(self, loop_id: str, after_sequence: int) -> dict:
        async with self._sessions() as session:
            access = await self._access.resolve(session, loop_id)
            sequence = await session.get(LoopJournalSequence, loop_id)
            last_sequence = int(sequence.last_sequence if sequence is not None else 0)
            if last_sequence - after_sequence > self._max_backlog:
                return {"status": "resync_required", "after_sequence": after_sequence, "last_sequence": last_sequence, "events": ()}
            try:
                events = await self._journal.read(session, loop_id, after_sequence, self._page_size)
            except ReplayUnavailable as exc:
                return {"status": "snapshot_required", "minimum_sequence": exc.minimum_sequence, "last_sequence": last_sequence, "events": ()}
            visible = [self._events.public_envelope(event, access.permissions).model_dump(mode="json") for event in events]
            next_sequence = events[-1].sequence if events else after_sequence
            return {"status": "events", "authority_revision": access.authority_revision, "after_sequence": after_sequence, "next_sequence": next_sequence, "last_sequence": last_sequence, "events": tuple(visible)}
