r"""本文件对外提供 IndexBudgetReservationRepository。

输入为独立 sessionmaker、Loop／授权版本、冻结限制及实际模型用量；输出为原子请求预留或预算阻断，以及幂等结算。
具体工作流为短事务锁住 Loop 用量，校验真实授权，扣除全部未结算预留；模型调用后另一短事务结算并保留审计凭据。
崩溃未结算的预留保持占用，不按时间自动回收；新授权不会清除既有用量或预留。
示例：await repository.reserve(input_upper_bound, output_limit); await repository.settle(delta)。不在模型调用期间持有 session。
"""

import uuid
from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import func, select

from backend.app.desktop.agent_loop.models import LoopBudgetUsage, LoopDelegationGrant
from backend.app.desktop.agent_loop.usage import LoopUsageLedger
from backend.app.desktop.agent_loop.resource_limits import exceeds_limit

from .index_model_budget import IndexBudgetExceeded
from .models import LoopIndexBudgetReservation


class IndexBudgetReservationRepository:
    _DEFAULTS: ClassVar[dict[str, int]] = {
        "model_calls": 200,
        "input_tokens": 2_000_000,
        "output_tokens": 500_000,
    }

    def __init__(self, sessions, loop_id, grant_revision, limits, frozen_usage):
        self._sessions = sessions
        self._loop_id = loop_id
        self._grant_revision = grant_revision
        self._limits = dict(limits)
        self._frozen_usage = dict(frozen_usage)
        self._reservation_id = uuid.uuid4().hex

    @classmethod
    async def authorization_snapshot(cls, sessions, loop_id, grant_revision):
        async with sessions.begin() as session:
            grant = await cls._authorized_grant(session, loop_id, grant_revision)
            usage = await session.get(LoopBudgetUsage, loop_id)
            if usage is None:
                raise IndexBudgetExceeded(
                    "semantic index authority usage ledger 不存在"
                )
            return {
                "limits": dict(grant.budgets),
                "usage": {name: getattr(usage, name) for name in cls._DEFAULTS},
            }

    async def reserve(self, input_tokens: int, output_tokens: int) -> None:
        amounts = {
            "model_calls": 1,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        async with self._sessions.begin() as session:
            usage = await self._locked_usage(session)
            grant = await self._authorized_grant(
                session, self._loop_id, self._grant_revision
            )
            pending = (
                await session.execute(
                    select(
                        *(
                            func.coalesce(
                                func.sum(getattr(LoopIndexBudgetReservation, name)), 0
                            )
                            for name in self._DEFAULTS
                        )
                    ).where(
                        LoopIndexBudgetReservation.loop_id == self._loop_id,
                        LoopIndexBudgetReservation.settled_at.is_(None),
                    )
                )
            ).one()
            for (name, default), reserved in zip(
                self._DEFAULTS.items(), pending, strict=True
            ):
                ceilings = (
                    self._limits.get(f"max_{name}", default),
                    grant.budgets.get(f"max_{name}", default),
                )
                consumed = max(
                    getattr(usage, name), int(self._frozen_usage.get(name, 0))
                )
                if any(exceeds_limit(consumed + reserved + amounts[name], limit) for limit in ceilings):
                    raise IndexBudgetExceeded(
                        "semantic index authorized_model_budget exhausted"
                    )
            row = await session.get(LoopIndexBudgetReservation, self._reservation_id)
            if row is None:
                row = LoopIndexBudgetReservation(
                    reservation_id=self._reservation_id,
                    loop_id=self._loop_id,
                    grant_revision=self._grant_revision,
                    model_calls=0,
                    input_tokens=0,
                    output_tokens=0,
                )
                session.add(row)
            if row.settled_at is not None:
                raise ValueError("settled index budget reservation cannot be reused")
            for name, amount in amounts.items():
                setattr(row, name, getattr(row, name) + amount)

    async def settle(self, delta) -> None:
        async with self._sessions.begin() as session:
            usage = await self._locked_usage(session)
            row = await session.get(LoopIndexBudgetReservation, self._reservation_id)
            if row is None or row.settled_at is not None:
                return
            LoopUsageLedger.apply(usage, delta)
            row.actual_usage = {
                name: getattr(delta, name) for name in (*self._DEFAULTS, "retries")
            }
            row.settled_at = datetime.now(UTC)

    async def _locked_usage(self, session):
        usage = await session.get(LoopBudgetUsage, self._loop_id, with_for_update=True)
        if usage is None:
            raise IndexBudgetExceeded("semantic index authority usage ledger 不存在")
        return usage

    @staticmethod
    async def _authorized_grant(session, loop_id, revision):
        grant = await session.scalar(
            select(LoopDelegationGrant).where(
                LoopDelegationGrant.loop_id == loop_id,
                LoopDelegationGrant.revision == revision,
                LoopDelegationGrant.status == "active",
            )
        )
        if grant is None or (
            grant.expires_at is not None and grant.expires_at <= datetime.now(UTC)
        ):
            raise IndexBudgetExceeded("semantic index authorization revision 已 stale")
        return grant
