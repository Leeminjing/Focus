r"""本文件对外提供 FactProjector 与 FactBackfillService 的可恢复物化入口。

输入为规范 journal、DesktopRun、projector cursor、checkpoint reader 和可选 Loop scope；输出为按 committed sequence 递增的
物化事实、tool/fact 事件及 backfill 计数。具体工作流为每次冻结 Loop journal 边界，按序消费 Run、Tool、Workspace、Artifact、
Context revision、Directive 与 verification 事件，在同一事务中提交事实和 cursor；Run settled 继续解析完整证据，backfill 复用同一 materializer。示例：
`count = await projector.reconcile(loop_id)`。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.fact_materializer import FactMaterializer
from backend.app.desktop.agent_loop.fact_event_materializer import FactEventMaterializer
from backend.app.desktop.agent_loop.journal_models import LoopJournalSequence, LoopProjectorCursor
from backend.app.desktop.agent_loop.materialized_fact_sources import RunFactSourceReader
from backend.app.desktop.models import DesktopRun


class FactProjector:
    PROJECTOR_NAME = "loop_facts"

    def __init__(self, sessions: async_sessionmaker[AsyncSession], checkpointer, page_size: int = 100) -> None:
        self._sessions = sessions
        self._checkpointer = checkpointer
        self._journal = LoopEventJournal()
        self._materializer = FactMaterializer(RunFactSourceReader(checkpointer))
        self._event_materializer = FactEventMaterializer()
        self._page_size = max(1, min(page_size, 500))

    async def drain(self) -> int:
        async with self._sessions() as session:
            loop_ids = tuple((await session.scalars(select(LoopJournalSequence.loop_id).order_by(LoopJournalSequence.loop_id))).all())
        processed = 0
        for loop_id in loop_ids:
            processed += await self.project_loop(loop_id)
        return processed

    async def reconcile(self, loop_id: str | None = None) -> int:
        if loop_id is None:
            async with self._sessions() as session:
                loop_ids = tuple((await session.scalars(select(DesktopRun.loop_id).where(DesktopRun.loop_id.is_not(None)).distinct().order_by(DesktopRun.loop_id))).all())
        else:
            loop_ids = (loop_id,)
        backfill = FactBackfillService(self._sessions, self._checkpointer)
        processed = 0
        for loop_id in loop_ids:
            processed += await backfill.run(loop_id)
        return processed

    async def project_loop(self, loop_id: str) -> int:
        processed = 0
        while True:
            async with self._sessions.begin() as session:
                cursor = await self._cursor(session, loop_id)
                sequence = await session.get(LoopJournalSequence, loop_id)
                boundary = int(sequence.last_sequence if sequence is not None else 0)
                if cursor.last_sequence >= boundary:
                    return processed
                page = tuple(event for event in await self._journal.read(session, loop_id, cursor.last_sequence, self._page_size) if event.sequence <= boundary)
                if not page:
                    return processed
                for event in page:
                    if event.kind == "context.run.settled":
                        run = await session.get(DesktopRun, event.entity_id)
                        if run is not None:
                            await self._materializer.materialize_run(session, run, event)
                            processed += 1
                    if await self._event_materializer.materialize(session, event) is not None:
                        processed += 1
                    cursor.last_sequence = event.sequence
            if len(page) < self._page_size:
                return processed

    @staticmethod
    async def _cursor(session: AsyncSession, loop_id: str) -> LoopProjectorCursor:
        await session.execute(insert(LoopProjectorCursor).values(loop_id=loop_id, projector_name=FactProjector.PROJECTOR_NAME, last_sequence=0).on_conflict_do_nothing(index_elements=["loop_id", "projector_name"]))
        return await session.get(LoopProjectorCursor, (loop_id, FactProjector.PROJECTOR_NAME), with_for_update=True)


class FactBackfillService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], checkpointer) -> None:
        self._sessions = sessions
        self._materializer = FactMaterializer(RunFactSourceReader(checkpointer))

    async def run(self, loop_id: str) -> int:
        async with self._sessions.begin() as session:
            runs = tuple((await session.scalars(select(DesktopRun).where(DesktopRun.loop_id == loop_id).order_by(DesktopRun.created_at, DesktopRun.run_id).with_for_update())).all())
            for run in runs:
                await self._materializer.materialize_run(session, run, None)
            return len(runs)
