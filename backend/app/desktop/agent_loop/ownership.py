r"""本文件对外提供 LoopFencingGuard 与 KernelFencingRejected。

输入为数据库事务、round identity 和十进制单调 fencing token；输出为通过验证的当前 owner 身份或拒绝异常。
具体工作流为初次 Kernel 提交同时核对 counter、活动 lease 与过期时间，长操作返回后的最终提交只核对持久
counter 是否仍等于原 token，从而拒绝已被新 owner 取代的结果。示例：`await guard.validate_active(session, round_id, token)`。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.models import LoopCoordinatorFence, LoopCoordinatorLease


class KernelFencingRejected(RuntimeError):
    pass


class LoopFencingGuard:
    async def validate_active(self, session: AsyncSession, round_id: str, token: int) -> None:
        await self.validate_current(session, round_id, token)
        lease = await session.scalar(
            select(LoopCoordinatorLease)
            .where(LoopCoordinatorLease.round_id == round_id)
            .with_for_update()
        )
        if lease is None or self.parse(lease.fencing_token) != token:
            raise KernelFencingRejected("coordinator_lease_changed")
        expires_at = lease.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise KernelFencingRejected("coordinator_lease_expired")

    async def validate_current(self, session: AsyncSession, round_id: str, token: int) -> None:
        if token <= 0:
            raise KernelFencingRejected("fencing_token_missing")
        fence = await session.get(LoopCoordinatorFence, round_id, with_for_update=True)
        if fence is None or fence.fencing_token != token:
            raise KernelFencingRejected("fencing_token_superseded")

    @staticmethod
    def parse(value: str) -> int:
        try:
            token = int(value)
        except (TypeError, ValueError) as exc:
            raise KernelFencingRejected("fencing_token_invalid") from exc
        if token <= 0:
            raise KernelFencingRejected("fencing_token_invalid")
        return token
