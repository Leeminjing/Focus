r"""本文件对外提供 RunOutboxRepository 与可重启 RunOutboxConsumer。

输入为事务内 MainRunSettled 事实、consumer identity、可选 Loop scope 和领域 handler；输出为安全规范化的稳定事件、领取结果与
首次消费布尔值。具体工作流为 finalizer 同事务 enqueue，每轮 drain 回收过期 claim，以 event row lock +
delivery receipt 去重处理；异常回滚后按 consumer/delivery_count 归还本次领取，持久化指数退避，
耗尽重试进入 failed。活跃处理事务持有行锁，恢复跳过锁定事件；进程内 wake 只降低延迟而不承载事实。
示例：`count = await consumer.drain("loop-coordinator", handler)`。
ArtifactVersion 使用同一 outbox/receipt 按 result_key 发布运行中文件来源；MainRunSettled 继续按 Run 唯一。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import inspect
import logging
from typing import Awaitable, Callable
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.run_orchestration.models import RunOutboxDelivery, RunOutboxEvent
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.persistence_safety import PersistencePayloadNormalizer


RunEventHandler = Callable[[RunOutboxEvent, AsyncSession], Awaitable[None] | None]
logger = logging.getLogger(__name__)


class RunOutboxRepository:
    def __init__(
        self,
        *,
        lease_seconds: float = 60.0,
        retry_seconds: float = 1.0,
        max_retry_seconds: float = 60.0,
        max_attempts: int = 8,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_seconds <= 0 or retry_seconds <= 0 or max_retry_seconds <= 0 or max_attempts < 1:
            raise ValueError("Outbox lease/retry settings must be positive")
        self._lease_seconds = lease_seconds
        self._retry_seconds = retry_seconds
        self._max_retry_seconds = max_retry_seconds
        self._max_attempts = max_attempts
        self._clock = clock or (lambda: datetime.now(UTC))

    async def enqueue_artifact(self, session, run_id, result_key):
        event_id = self.event_id(run_id, f"ArtifactVersion:{result_key}")
        event = await session.get(RunOutboxEvent, event_id)
        if event is None:
            event = RunOutboxEvent(event_id=event_id, run_id=run_id,
                event_type="ArtifactVersion", payload={"result_key": result_key}, status="pending")
            session.add(event)
            await session.flush()
        return event

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
        normalized = PersistencePayloadNormalizer.normalize(payload, "run-outbox.payload")
        safe_payload = normalized.value
        if normalized.replacement_count:
            safe_payload = {**safe_payload, "_persistence_safety": normalized.metadata()}
        event = RunOutboxEvent(
            event_id=event_id,
            run_id=run_id,
            event_type="MainRunSettled",
            payload=safe_payload,
            status="pending",
        )
        session.add(event)
        await session.flush()
        return event

    async def claim_next(
        self,
        session: AsyncSession,
        consumer_id: str,
        loop_id: str | None = None,
    ) -> RunOutboxEvent | None:
        now = self._clock()
        statement = (
            select(RunOutboxEvent)
            .join(DesktopRun, DesktopRun.run_id == RunOutboxEvent.run_id)
            .where(or_(RunOutboxEvent.status == "pending", RunOutboxEvent.status == "error"))
            .where(or_(RunOutboxEvent.next_attempt_at.is_(None), RunOutboxEvent.next_attempt_at <= now))
            .order_by(RunOutboxEvent.created_at, RunOutboxEvent.event_id)
            .with_for_update(of=RunOutboxEvent, skip_locked=True)
            .limit(1)
        )
        if loop_id is not None:
            statement = statement.where(DesktopRun.loop_id == loop_id)
        event = await session.scalar(statement)
        if event is None:
            return None
        event.status = "claimed"
        event.claimed_by = consumer_id
        event.claimed_at = now
        event.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
        event.next_attempt_at = None
        event.delivery_count += 1
        await session.flush()
        return event

    async def deliver_once(
        self,
        session: AsyncSession,
        event_id: str,
        consumer_id: str,
        handler: RunEventHandler,
        *,
        claim_attempt: int,
    ) -> bool:
        event = await session.scalar(
            select(RunOutboxEvent)
            .where(RunOutboxEvent.event_id == event_id)
            .with_for_update()
        )
        if event is None:
            raise LookupError(f"Run outbox event 不存在: {event_id}")
        if (
            event.status != "claimed"
            or event.claimed_by != consumer_id
            or event.delivery_count != claim_attempt
            or event.lease_expires_at is None
            or event.lease_expires_at <= self._clock()
        ):
            return False
        receipt = await session.scalar(
            select(RunOutboxDelivery.delivery_id).where(
                RunOutboxDelivery.event_id == event_id,
                RunOutboxDelivery.consumer_id == consumer_id,
            )
        )
        if receipt is not None:
            self._mark_delivered(event)
            await session.flush()
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
        self._mark_delivered(event)
        await session.flush()
        return True

    async def fail_claim(
        self,
        session: AsyncSession,
        event_id: str,
        consumer_id: str,
        claim_attempt: int,
        error: str,
    ) -> bool:
        event = await session.scalar(
            select(RunOutboxEvent)
            .where(
                RunOutboxEvent.event_id == event_id,
                RunOutboxEvent.status == "claimed",
                RunOutboxEvent.claimed_by == consumer_id,
                RunOutboxEvent.delivery_count == claim_attempt,
            )
            .with_for_update()
        )
        if event is None:
            return False
        self._record_failure(event, error)
        await session.flush()
        return True

    async def release_claims(self, session: AsyncSession, loop_id: str | None = None) -> int:
        statement = (
            select(RunOutboxEvent)
            .where(RunOutboxEvent.status == "claimed")
            .where(or_(RunOutboxEvent.lease_expires_at.is_(None), RunOutboxEvent.lease_expires_at <= self._clock()))
            .with_for_update(of=RunOutboxEvent, skip_locked=True)
        )
        if loop_id is not None:
            statement = statement.join(DesktopRun, DesktopRun.run_id == RunOutboxEvent.run_id).where(DesktopRun.loop_id == loop_id)
        events = list(await session.scalars(statement))
        for event in events:
            legacy_claim = event.lease_expires_at is None
            self._record_failure(event, "claim lease expired")
            if legacy_claim and event.status == "error":
                event.next_attempt_at = self._clock()
        await session.flush()
        return len(events)

    def _record_failure(self, event: RunOutboxEvent, error: str) -> None:
        event.status = "failed" if event.delivery_count >= self._max_attempts else "error"
        event.error = error[:1000]
        self._clear_claim(event)
        delay = min(self._retry_seconds * 2 ** min(max(event.delivery_count - 1, 0), 30), self._max_retry_seconds)
        event.next_attempt_at = self._clock() + timedelta(seconds=delay) if event.status == "error" else None

    def _mark_delivered(self, event: RunOutboxEvent) -> None:
        event.status = "delivered"
        event.delivered_at = self._clock()
        event.error = None
        event.next_attempt_at = None
        self._clear_claim(event)

    @staticmethod
    def _clear_claim(event: RunOutboxEvent) -> None:
        event.claimed_by = None
        event.claimed_at = None
        event.lease_expires_at = None

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

    async def recover(self, *, loop_id: str | None = None) -> int:
        async with self._sessions.begin() as session:
            return await self._repository.release_claims(session, loop_id)

    async def drain(
        self,
        consumer_id: str,
        handler: RunEventHandler,
        *,
        limit: int = 100,
        loop_id: str | None = None,
    ) -> int:
        await self.recover(loop_id=loop_id)
        delivered = 0
        for _ in range(limit):
            event_id, claim_attempt = None, None
            try:
                async with self._sessions.begin() as session:
                    event = await self._repository.claim_next(session, consumer_id, loop_id)
                    if event is None:
                        break
                    # Capture identity before commit, including with expire_on_commit=True.
                    event_id, claim_attempt = event.event_id, event.delivery_count
                async with self._sessions.begin() as session:
                    applied = await self._repository.deliver_once(
                        session, event_id, consumer_id, handler, claim_attempt=claim_attempt
                    )
                delivered += int(applied)
            except (Exception, asyncio.CancelledError) as exc:
                if event_id is not None:
                    try:
                        # The handler transaction has rolled back; return only this claim.
                        async with self._sessions.begin() as session:
                            await self._repository.fail_claim(
                                session, event_id, consumer_id, claim_attempt,
                                f"{type(exc).__name__}: {exc}",
                            )
                    except Exception:
                        # Lease recovery remains available if the database is unavailable.
                        logger.exception("Failed to return Run outbox claim: %s", event_id)
                raise
        return delivered
