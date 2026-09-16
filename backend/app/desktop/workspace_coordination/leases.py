r"""本文件对外提供 WorkspaceLeaseManager 与 WorkspaceLeaseConflict。

输入为 slot、Run、服务端推导的访问模式和期限；输出为可续约 lease 或确定性并发拒绝。
具体工作流为按 slot 行锁回收过期租约、允许 Reader 共享稳定版本但令 Writer 与全部其他 lease 互斥、
递增 fencing token 并以部分唯一索引兜底，工具调用前用 assert_valid 拒绝过期或陈旧 token。
示例：`grant = await manager.acquire(request)`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.workspace_coordination.models import WorkspaceLease, WorkspaceSlot
from backend.app.desktop.workspace_coordination.schemas import WorkspaceLeaseGrant, WorkspaceLeaseRequest


class WorkspaceLeaseConflict(RuntimeError):
    pass


class WorkspaceLeaseManager:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def acquire(self, request: WorkspaceLeaseRequest) -> WorkspaceLeaseGrant:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            slot = await session.scalar(
                select(WorkspaceSlot).where(WorkspaceSlot.slot_id == request.slot_id).with_for_update()
            )
            if slot is None or slot.lifecycle != "active":
                raise WorkspaceLeaseConflict("workspace slot 不可用")
            await self._expire(session, request.slot_id, now)
            existing = await session.scalar(select(WorkspaceLease).where(WorkspaceLease.run_id == request.run_id))
            if existing is not None and existing.status == "active" and existing.expires_at > now:
                if existing.slot_id != request.slot_id or existing.mode != request.mode.value:
                    raise WorkspaceLeaseConflict("Run 已绑定不同的 workspace lease")
                return self._grant(existing)
            incompatible = await session.scalar(
                select(WorkspaceLease.lease_id).where(
                    WorkspaceLease.slot_id == request.slot_id,
                    WorkspaceLease.status == "active",
                    (
                        WorkspaceLease.mode == "write"
                        if request.mode.value == "read"
                        else WorkspaceLease.mode.in_(["read", "write"])
                    ),
                ).limit(1)
            )
            if incompatible is not None:
                raise WorkspaceLeaseConflict("workspace slot 存在不兼容的活动 lease")
            slot.fencing_counter += 1
            lease = WorkspaceLease(
                lease_id=uuid.uuid4().hex,
                slot_id=request.slot_id,
                run_id=request.run_id,
                mode=request.mode.value,
                fencing_token=slot.fencing_counter,
                expires_at=now + timedelta(seconds=request.ttl_seconds),
            )
            session.add(lease)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise WorkspaceLeaseConflict("workspace slot 已有活动 writer") from exc
            return self._grant(lease)

    async def renew(self, lease_id: str, fencing_token: int, ttl_seconds: int = 120) -> WorkspaceLeaseGrant:
        expired = False
        grant = None
        async with self._sessions.begin() as session:
            lease = await session.scalar(select(WorkspaceLease).where(WorkspaceLease.lease_id == lease_id).with_for_update())
            self._require_token(lease, fencing_token)
            now = datetime.now(UTC)
            if lease.expires_at <= now:
                lease.status = "expired"
                expired = True
            else:
                lease.expires_at = now + timedelta(seconds=ttl_seconds)
                grant = self._grant(lease)
        if expired:
            raise WorkspaceLeaseConflict("workspace lease 已过期，不能续约")
        return grant

    async def release(self, lease_id: str, fencing_token: int) -> None:
        async with self._sessions.begin() as session:
            lease = await session.scalar(select(WorkspaceLease).where(WorkspaceLease.lease_id == lease_id).with_for_update())
            self._require_token(lease, fencing_token)
            lease.status = "released"
            lease.released_at = datetime.now(UTC)

    async def assert_valid(self, lease_id: str, fencing_token: int) -> WorkspaceLeaseGrant:
        failure = None
        grant = None
        async with self._sessions.begin() as session:
            lease = await session.scalar(select(WorkspaceLease).where(WorkspaceLease.lease_id == lease_id).with_for_update())
            self._require_token(lease, fencing_token)
            if lease.expires_at <= datetime.now(UTC):
                lease.status = "expired"
                failure = "workspace lease 已过期"
            else:
                slot = await session.get(WorkspaceSlot, lease.slot_id)
                if slot is None or (
                    lease.mode == "write" and slot.fencing_counter != fencing_token
                ):
                    lease.status = "fenced"
                    failure = "workspace fencing token 已陈旧"
                else:
                    grant = self._grant(lease)
        if failure is not None:
            raise WorkspaceLeaseConflict(failure)
        return grant

    @staticmethod
    async def _expire(session: AsyncSession, slot_id: str, now: datetime) -> None:
        await session.execute(
            update(WorkspaceLease)
            .where(WorkspaceLease.slot_id == slot_id, WorkspaceLease.status == "active", WorkspaceLease.expires_at <= now)
            .values(status="expired")
        )

    @staticmethod
    def _require_token(lease: WorkspaceLease | None, token: int) -> None:
        if lease is None or lease.status != "active" or lease.fencing_token != token:
            raise WorkspaceLeaseConflict("workspace lease 不存在、已释放或 token 无效")

    @staticmethod
    def _grant(lease: WorkspaceLease) -> WorkspaceLeaseGrant:
        return WorkspaceLeaseGrant(
            lease_id=lease.lease_id,
            slot_id=lease.slot_id,
            run_id=lease.run_id,
            mode=lease.mode,
            fencing_token=lease.fencing_token,
            expires_at=lease.expires_at.isoformat(),
        )
