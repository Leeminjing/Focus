r"""本文件对外提供 RunOutboxRepository 与可重启 RunOutboxConsumer。

输入为事务内 MainRunSettled 事实、consumer identity 和领域 handler；输出为稳定事件、领取结果与
首次消费布尔值。具体工作流为 finalizer 同事务 enqueue，consumer 启动先释放遗留 claim，再直接
扫描数据库并以 event row lock + delivery receipt 去重处理；进程内 wake 只降低延迟而不承载事实。
示例：`count = await consumer.drain("loop-coordinator", handler)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
import inspect
from typing import Awaitable, Callable
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.run_orchestration.models import RunOutboxDelivery, RunOutboxEvent


RunEventHandler = Callable[[RunOutboxEvent, AsyncSession], Awaitable[None] | None]


class RunOutboxRepository:
    async def enqueue_settled(
        self,
        session: AsyncSession,
        run_id: str,
        payload: dict,
    ) -> RunOutboxEvent:
        event_id = self.event_id(run_id, "MainRunSettled")
        event = await session.get(RunOutboxEvent, event_id)
        if event is not None:
            return event
        event = RunOutboxEvent(
            event_id=event_id,
            run_id=run_id,
            event_type="MainRunSettled",
            payload=payload,
            status="pending",
        )
        session.add(event)
        await session.flush()
        return event

    async def claim_next(
        self,
        session: AsyncSession,
        consumer_id: str,
    ) -> RunOutboxEvent | None:
        event = await session.scalar(
            select(RunOutboxEvent)
            .where(or_(RunOutboxEvent.status == "pending", RunOutboxEvent.status == "error"))
            .order_by(RunOutboxEvent.created_at, RunOutboxEvent.event_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if event is None:
            return None
        event.status = "claimed"
        event.claimed_by = consumer_id
        event.claimed_at = datetime.now(UTC)
        event.delivery_count += 1
        event.error = None
        await session.flush()
        return event

    async def deliver_once(
        self,
        session: AsyncSession,
        event_id: str,
        consumer_id: str,
        handler: RunEventHandler,
    ) -> bool:
        event = await session.scalar(
            select(RunOutboxEvent)
            .where(RunOutboxEvent.event_id == event_id)
            .with_for_update()
        )
        if event is None:
            raise LookupError(f"Run outbox event 不存在: {event_id}")
        receipt = await session.scalar(
            select(RunOutboxDelivery.delivery_id).where(
                RunOutboxDelivery.event_id == event_id,
                RunOutboxDelivery.consumer_id == consumer_id,
            )
        )
        if receipt is not None:
            return False
        handled = handler(event, session)
        if inspect.isawaitable(handled):
            await handled
        session.add(
            RunOutboxDelivery(
                delivery_id=uuid.uuid4().hex,
                event_id=event_id,
                consumer_id=consumer_id,
            )
        )
        event.status = "delivered"
        event.delivered_at = datetime.now(UTC)
        event.error = None
        await session.flush()
        return True

    async def release_claims(self, session: AsyncSession) -> int:
        result = await session.execute(
            update(RunOutboxEvent)
            .where(RunOutboxEvent.status == "claimed")
            .values(status="error", claimed_by=None, claimed_at=None, error="consumer restarted")
        )
        return int(result.rowcount or 0)

    @staticmethod
    def event_id(run_id: str, event_type: str) -> str:
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"focus:run-outbox:{run_id}:{event_type}",
        ).hex


class RunOutboxConsumer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository: RunOutboxRepository | None = None,
    ) -> None:
        self._sessions = session_factory
        self._repository = repository or RunOutboxRepository()

    async def recover(self) -> int:
        async with self._sessions.begin() as session:
            return await self._repository.release_claims(session)

    async def drain(
        self,
        consumer_id: str,
        handler: RunEventHandler,
        *,
        limit: int = 100,
    ) -> int:
        delivered = 0
        for _ in range(limit):
            async with self._sessions.begin() as session:
                event = await self._repository.claim_next(session, consumer_id)
                if event is None:
                    break
                event_id = event.event_id
            async with self._sessions.begin() as session:
                if await self._repository.deliver_once(
                    session, event_id, consumer_id, handler
                ):
                    delivered += 1
        return delivered
