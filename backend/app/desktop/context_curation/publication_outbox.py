r"""本文件对外提供 Portfolio publication 结果的事务型 outbox 与消费去重端口。

输入为 Portfolio aggregate、稳定事件类型、payload、consumer 和事务内处理函数；输出为唯一事件、
可领取事件或是否首次消费。具体工作流为权威提交中用确定性 ID 写事件，消费者以 skip-locked 领取，
再把数据库副作用与 delivery receipt 同事务提交；重复投递命中唯一 receipt 后不再执行处理函数。
示例：`applied = await outbox.deliver_once(session, event_id, "loop", handler)`。
"""

from __future__ import annotations

from datetime import UTC, datetime
import inspect
from typing import Any, Awaitable, Callable
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.context_curation.models import (
    CurationOutboxDelivery,
    CurationOutboxEvent,
    CurationOutboxStatus,
)


DeliveryHandler = Callable[[CurationOutboxEvent], Awaitable[None] | None]


class CurationOutboxRepository:
    async def enqueue(
        self,
        session: AsyncSession,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> CurationOutboxEvent:
        event_id = self.event_id(aggregate_id, event_type)
        existing = await session.get(CurationOutboxEvent, event_id)
        if existing is not None:
            if existing.payload != payload:
                raise ValueError("同一 Portfolio 结果事件不得改写 payload")
            return existing
        event = CurationOutboxEvent(
            event_id=event_id,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            status=CurationOutboxStatus.PENDING.value,
        )
        session.add(event)
        await session.flush()
        return event

    async def claim_next(
        self,
        session: AsyncSession,
        consumer_id: str,
    ) -> CurationOutboxEvent | None:
        event = await session.scalar(
            select(CurationOutboxEvent)
            .where(
                or_(
                    CurationOutboxEvent.status == CurationOutboxStatus.PENDING.value,
                    CurationOutboxEvent.status == CurationOutboxStatus.ERROR.value,
                )
            )
            .order_by(CurationOutboxEvent.created_at, CurationOutboxEvent.event_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if event is None:
            return None
        event.status = CurationOutboxStatus.CLAIMED.value
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
        handler: DeliveryHandler,
    ) -> bool:
        event = await session.scalar(
            select(CurationOutboxEvent)
            .where(CurationOutboxEvent.event_id == event_id)
            .with_for_update()
        )
        if event is None:
            raise LookupError(f"outbox event 不存在: {event_id}")
        delivered = await session.scalar(
            select(CurationOutboxDelivery.delivery_id).where(
                CurationOutboxDelivery.event_id == event_id,
                CurationOutboxDelivery.consumer_id == consumer_id,
            )
        )
        if delivered is not None:
            return False
        result = handler(event)
        if inspect.isawaitable(result):
            await result
        session.add(
            CurationOutboxDelivery(
                delivery_id=uuid.uuid4().hex,
                event_id=event_id,
                consumer_id=consumer_id,
            )
        )
        event.status = CurationOutboxStatus.DELIVERED.value
        event.delivered_at = datetime.now(UTC)
        event.error = None
        await session.flush()
        return True

    async def fail(
        self,
        session: AsyncSession,
        event_id: str,
        error: str,
    ) -> None:
        event = await session.get(CurationOutboxEvent, event_id)
        if event is None:
            raise LookupError(f"outbox event 不存在: {event_id}")
        event.status = CurationOutboxStatus.ERROR.value
        event.error = error
        await session.flush()

    @staticmethod
    def event_id(aggregate_id: str, event_type: str) -> str:
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"focus:curation-outbox:{aggregate_id}:{event_type}",
        ).hex
