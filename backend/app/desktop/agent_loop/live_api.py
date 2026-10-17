r"""本文件对外提供 LoopLiveSnapshotService.read 与 LoopLiveEventFeed.read_batch。

输入为 Loop identity、journal cursor、当前授权和物化领域实体；输出为单 sequence 边界完整 snapshot、已脱敏且 sequence 连续的有序事件批次、
snapshot-required 或 resync-required 协议结果。具体工作流为 snapshot 在调用方稳定的只读事务内读取一次 per-Loop sequence 边界，再组合数据库 current state 与
有界 current entity；历史活动通过独立分页接口读取。Loop/current Context state 同时携带渲染所需的授权、预算和 membership 字段；feed 每批复查授权、限制 backlog/page，
未授权事件转换为无敏感 identity 的同 sequence 占位信封，避免客户端 cursor 卡住或误判 gap。
检查点保留 reducer 原始版本并按 current entity 集合裁剪，不能缓存 overlay 派生版本或用户脱敏结果；读取事务结束后独立单调保存，缓存写失败不阻断读取。
示例：`batch = await feed.read_batch(loop_id, 12)`。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.event_contract import LiveEventAuthorizationPolicy
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal, ReplayUnavailable
from backend.app.desktop.agent_loop.journal_models import LoopJournalSequence
from backend.app.desktop.agent_loop.live_access import LoopLiveAccessPolicy, LoopLiveRedactionPolicy
from backend.app.desktop.agent_loop.live_projection_projector import LoopLiveSnapshotProjector
from backend.app.desktop.agent_loop.live_snapshot_overlay import LoopLiveProjectionOverlay
from backend.app.desktop.agent_loop.live_checkpoint import LoopLiveCheckpointStore
from backend.app.desktop.agent_loop.live_projection_reducer import LoopLiveProjectionReducer


class LoopLiveSnapshotService:
    def __init__(self, projector: LoopLiveSnapshotProjector | None = None) -> None:
        self._projector = projector or LoopLiveSnapshotProjector()
        self._access = LoopLiveAccessPolicy()
        self._overlay = LoopLiveProjectionOverlay()
        self._checkpoint = None

    async def read(self, session: AsyncSession, loop_id: str) -> dict:
        sequence = await session.get(LoopJournalSequence, loop_id)
        if sequence is None:
            raise HTTPException(404, "Loop 尚无 Live journal")
        boundary = int(sequence.last_sequence)
        access = await self._access.resolve(session, loop_id)
        projected = await self._projector.project(session, loop_id, boundary=boundary)
        projection = await self._overlay.apply(session, projected, boundary)
        collections = tuple(LoopLiveProjectionReducer._COLLECTIONS.values())
        self._checkpoint = projected.model_copy(update={
            **{name: {key: value for key, value in getattr(projected, name).items()
                      if key in getattr(projection, name)} for name in collections},
            "activity_timeline": (),
        })
        for name in ("loop", "accounting"):
            entity = getattr(self._checkpoint, name)
            if entity is not None and isinstance(entity.state.get("accounting"), dict):
                entity = entity.model_copy(update={"state": {**entity.state,
                    "accounting": {**entity.state["accounting"], "round_history": []}}})
                self._checkpoint = self._checkpoint.model_copy(update={name: entity})
        return LoopLiveRedactionPolicy.apply(projection.model_copy(update={"activity_timeline": ()}), access.permissions).model_dump(mode="json")

    async def save_checkpoint(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        if self._checkpoint is None:
            return
        try:
            async with sessions.begin() as session:
                await LoopLiveCheckpointStore().save(session, self._checkpoint)
        except SQLAlchemyError:
            logging.getLogger(__name__).warning("Live checkpoint write failed", exc_info=True)


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
