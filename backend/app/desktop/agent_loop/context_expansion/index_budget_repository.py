r"""本文件对外提供 IndexBudgetReservationRepository。

输入为独立 sessionmaker、Loop／授权版本、冻结限制及实际模型用量；输出为原子请求预留或预算阻断，以及幂等结算。
具体工作流为短事务先锁控制身份、核对类型化 owner 与 Worker 领取身份，再锁用量并扣除全部未结算预留；模型调用后另一短事务结算并保留审计凭据。
settle_in/mark_unknown_in接受调用方已有事务，使真实消费与Worker结果原子提交；不额外连接、提交或重试，回滚后原receipt可幂等重放。
from_receipt仅绑定调用方已读取的持久回执，用于可信owner恢复后的unknown分类及迟到实际回填，不创建预留。
崩溃未结算的预留保持占用，不按时间自动回收；新授权不会清除既有用量或预留；每次转移同事务发布共享账本的独立消费读模型。
示例：await repository.reserve(input_upper_bound, output_limit); await repository.settle(delta)。不在模型调用期间持有 session。
"""

import uuid
from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import func, select

from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopDelegationGrant
from backend.app.desktop.agent_loop.usage import LoopUsageLedger
from backend.app.desktop.agent_loop.resource_limits import exceeds_limit

from .index_model_budget import IndexBudgetExceeded
from .models import LoopIndexBudgetReservation


class IndexBudgetReservationRepository:
    _DEFAULTS: ClassVar[dict[str, None]] = {
        "model_calls": None,
        "input_tokens": None,
        "output_tokens": None,
    }

    def __init__(self, sessions, loop_id, grant_revision, limits, frozen_usage, *, owner=None):
        self._sessions = sessions
        self._loop_id = loop_id
        self._grant_revision = grant_revision
        self._limits = dict(limits)
        self._frozen_usage = dict(frozen_usage)
        self._reservation_id = uuid.uuid4().hex
        self._owner = dict(owner or {})

    @classmethod
    def from_receipt(cls, sessions, receipt):
        repository = cls(sessions, receipt.loop_id, receipt.grant_revision, {}, {}, owner=receipt.actual_usage)
        repository._reservation_id = receipt.reservation_id
        return repository

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
            grant = await self._authorized_grant(
                session, self._loop_id, self._grant_revision
            )
            await self._validate_owner(session)
            usage = await self._locked_usage(session)
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
                    actual_usage=self._owner or None,
                )
                session.add(row)
            if row.settled_at is not None:
                raise ValueError("settled index budget reservation cannot be reused")
            for name, amount in amounts.items():
                setattr(row, name, getattr(row, name) + amount)
            await self._record_accounting(session, "reserved")

    async def settle(self, delta) -> None:
        async with self._sessions.begin() as session:
            await self.settle_in(session, delta)

    async def settle_in(self, session, delta) -> None:
        usage = await self._locked_usage(session)
        row = await session.get(LoopIndexBudgetReservation, self._reservation_id,
                                with_for_update=True, populate_existing=True)
        if row is None or row.settled_at is not None:
            return
        LoopUsageLedger.apply(usage, delta)
        row.actual_usage = {**(row.actual_usage or {}), **{
            name: getattr(delta, name) for name in (*self._DEFAULTS, "retries")
        }}
        row.settled_at = datetime.now(UTC)
        await self._record_accounting(session, "actual")

    async def mark_unknown(self) -> None:
        async with self._sessions.begin() as session:
            await self.mark_unknown_in(session)

    async def mark_unknown_in(self, session) -> None:
        await self._locked_usage(session)
        row = await session.get(LoopIndexBudgetReservation, self._reservation_id,
                                with_for_update=True, populate_existing=True)
        if row is not None and row.settled_at is None and (row.actual_usage or {}).get("receipt_state") != "unknown":
            row.actual_usage = {**(row.actual_usage or {}), "receipt_state": "unknown"}
            await self._record_accounting(session, "unknown")

    async def _record_accounting(self, session, transition):
        from backend.app.desktop.agent_loop.accounting_events import LoopAccountingEventRecorder

        await LoopAccountingEventRecorder().record(session, self._loop_id,
            source_kind="index_receipt", source_id=self._reservation_id, transition=transition)

    async def _locked_usage(self, session):
        usage = await session.get(LoopBudgetUsage, self._loop_id, with_for_update=True, populate_existing=True)
        if usage is None:
            raise IndexBudgetExceeded("semantic index authority usage ledger 不存在")
        return usage

    async def _validate_owner(self, session):
        if not self._owner:
            return
        from backend.app.desktop.agent_loop.models import LoopPatrolAttempt, LoopRound, LoopWorkerRequest

        kind = self._owner.get("owner_kind")
        model = {"patrol": LoopPatrolAttempt, "worker": LoopWorkerRequest, "round": LoopRound}.get(kind)
        source = await session.get(model, self._owner.get("owner_id"), with_for_update=True) if model else None
        loop = await session.get(AgentLoop, self._loop_id)
        round_id = self._owner.get("round_id")
        if (source is None or loop.current_round_id != round_id
                or (source.loop_id, source.round_id) != (loop.loop_id, round_id)
                or (kind != "round" and source.status != "running")
                or (kind == "round" and (not source.observation_id or source.status in {"settled", "error", "superseded"}))):
            raise IndexBudgetExceeded("model owner 已取消或 superseded")
        if kind == "worker" and (not self._owner.get("retry_identity") or source.retry_identity != self._owner["retry_identity"]):
            raise IndexBudgetExceeded("model owner Worker attempt 已过期")

    @staticmethod
    async def _authorized_grant(session, loop_id, revision):
        loop = await session.get(AgentLoop, loop_id, with_for_update=True)
        if loop is None or loop.status != "running" or loop.authority_revision != revision:
            raise IndexBudgetExceeded("semantic index authorization revision 已 stale")
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
