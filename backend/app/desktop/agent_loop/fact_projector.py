r"""本文件对外提供 FactProjector 与 FactBackfillService 的故障隔离、可恢复物化入口。

输入为规范 journal、DesktopRun、projector cursor、checkpoint reader 和可选 Loop scope；输出为按 committed sequence 递增的
物化事实、tool/fact 事件、隔离失败、显式修复重放及 backfill 计数。具体工作流为冻结 journal 边界后以独立事务处理每个事件/Run，
确定性坏单元 quarantine 后显式越过，可重试失败保留边界；后台先恢复 journal cursor 再 backfill 且不阻塞应用 readiness。示例：`count = await projector.reconcile(loop_id)`。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.fact_materializer import FactMaterializer
from backend.app.desktop.agent_loop.fact_event_materializer import FactEventMaterializer
from backend.app.desktop.agent_loop.journal_models import LoopJournalSequence, LoopProjectorCursor
from backend.app.desktop.agent_loop.materialized_fact_sources import RunFactSourceReader
from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure
from backend.app.desktop.agent_loop.projection_recovery import ProjectionRecoveryRepository
from backend.app.desktop.models import DesktopRun


logger = logging.getLogger(__name__)


class FactProjector:
    PROJECTOR_NAME = "loop_facts"

    def __init__(self, sessions: async_sessionmaker[AsyncSession], checkpointer, page_size: int = 100) -> None:
        self._sessions = sessions
        self._checkpointer = checkpointer
        self._journal = LoopEventJournal()
        self._materializer = FactMaterializer(RunFactSourceReader(checkpointer))
        self._event_materializer = FactEventMaterializer()
        self._recovery = ProjectionRecoveryRepository()
        self._page_size = max(1, min(page_size, 500))
        self._reconcile_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._reconcile_task is None or self._reconcile_task.done():
            self._reconcile_task = asyncio.create_task(self._reconcile_background(), name="loop-fact-reconcile")

    async def close(self) -> None:
        if self._reconcile_task is None:
            return
        self._reconcile_task.cancel()
        await asyncio.gather(self._reconcile_task, return_exceptions=True)
        self._reconcile_task = None

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
        for current_loop_id in loop_ids:
            processed += await backfill.run(current_loop_id)
        return processed

    async def project_loop(self, loop_id: str) -> int:
        processed = 0
        while True:
            page = await self._next_page(loop_id)
            if not page:
                return processed
            for event in page:
                try:
                    processed += await self._project_event(loop_id, event)
                except Exception as error:
                    failure = await self._record_event_failure(loop_id, event, error)
                    if failure.status == "retryable":
                        return processed
            if len(page) < self._page_size:
                return processed

    async def repair_failure(self, failure_id: str) -> int:
        async with self._sessions.begin() as session:
            failure = await session.get(LoopProjectionFailure, failure_id, with_for_update=True)
            if failure is None:
                raise LookupError(failure_id)
            if failure.projector_name != self.PROJECTOR_NAME or failure.status != "quarantined":
                raise ValueError("只有 loop_facts 的 quarantined 单元可修复重放")
            loop_id = failure.loop_id
            if failure.unit_kind == "journal_event":
                cursor = await self._cursor(session, loop_id)
                cursor.last_sequence = min(cursor.last_sequence, max(0, failure.boundary_sequence - 1))
            elif failure.unit_kind != "run_backfill":
                raise ValueError(f"未知投影单元: {failure.unit_kind}")
            failure.status = "retryable"
            failure.resolved_at = None
            unit_kind = failure.unit_kind
            unit_id = failure.unit_id
        if unit_kind == "run_backfill":
            await FactBackfillService(self._sessions, self._checkpointer)._project_run(loop_id, unit_id)
            return 1
        return await self.project_loop(loop_id)

    async def _next_page(self, loop_id: str) -> tuple:
        async with self._sessions() as session:
            cursor = await self._cursor(session, loop_id)
            sequence = await session.get(LoopJournalSequence, loop_id)
            boundary = int(sequence.last_sequence if sequence is not None else 0)
            if cursor.last_sequence >= boundary:
                return ()
            return tuple(
                event
                for event in await self._journal.read(session, loop_id, cursor.last_sequence, self._page_size)
                if event.sequence <= boundary
            )

    async def _project_event(self, loop_id: str, event) -> int:
        processed = 0
        async with self._sessions.begin() as session:
            cursor = await self._cursor(session, loop_id)
            if cursor.last_sequence >= event.sequence:
                return 0
            if event.kind == "context.run.settled":
                run = await session.get(DesktopRun, event.entity_id)
                if run is not None:
                    await self._materializer.materialize_run(session, run, event)
                    processed += 1
            if await self._event_materializer.materialize(session, event) is not None:
                processed += 1
            cursor.last_sequence = event.sequence
            await self._recovery.resolve(
                session,
                loop_id=loop_id,
                projector_name=self.PROJECTOR_NAME,
                unit_kind="journal_event",
                unit_id=event.event_id,
                boundary_sequence=event.sequence,
            )
        return processed

    async def _record_event_failure(self, loop_id: str, event, error: Exception):
        async with self._sessions.begin() as session:
            failure = await self._recovery.record_failure(
                session,
                loop_id=loop_id,
                projector_name=self.PROJECTOR_NAME,
                unit_kind="journal_event",
                unit_id=event.event_id,
                error=error,
                boundary_sequence=event.sequence,
            )
            if failure.status == "quarantined":
                cursor = await self._cursor(session, loop_id)
                cursor.last_sequence = max(cursor.last_sequence, event.sequence)
            return failure

    async def _reconcile_background(self) -> None:
        try:
            await self.drain()
            await self.reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Loop fact background reconciliation failed")

    @staticmethod
    async def _cursor(session: AsyncSession, loop_id: str) -> LoopProjectorCursor:
        await session.execute(insert(LoopProjectorCursor).values(loop_id=loop_id, projector_name=FactProjector.PROJECTOR_NAME, last_sequence=0).on_conflict_do_nothing(index_elements=["loop_id", "projector_name"]))
        return await session.get(LoopProjectorCursor, (loop_id, FactProjector.PROJECTOR_NAME), with_for_update=True)


class FactBackfillService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], checkpointer) -> None:
        self._sessions = sessions
        self._materializer = FactMaterializer(RunFactSourceReader(checkpointer))
        self._recovery = ProjectionRecoveryRepository()

    async def run(self, loop_id: str) -> int:
        async with self._sessions() as session:
            run_ids = tuple((await session.scalars(select(DesktopRun.run_id).where(DesktopRun.loop_id == loop_id).order_by(DesktopRun.created_at, DesktopRun.run_id))).all())
        processed = 0
        for run_id in run_ids:
            try:
                await self._project_run(loop_id, run_id)
                processed += 1
            except Exception as error:
                async with self._sessions.begin() as session:
                    await self._recovery.record_failure(
                        session,
                        loop_id=loop_id,
                        projector_name=FactProjector.PROJECTOR_NAME,
                        unit_kind="run_backfill",
                        unit_id=run_id,
                        error=error,
                    )
        return processed

    async def _project_run(self, loop_id: str, run_id: str) -> None:
        async with self._sessions.begin() as session:
            run = await session.get(DesktopRun, run_id, with_for_update=True)
            if run is None:
                return
            await self._materializer.materialize_run(session, run, None)
            await self._recovery.resolve(
                session,
                loop_id=loop_id,
                projector_name=FactProjector.PROJECTOR_NAME,
                unit_kind="run_backfill",
                unit_id=run_id,
            )
