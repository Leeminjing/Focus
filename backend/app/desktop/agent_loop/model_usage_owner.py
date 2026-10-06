"""本文件对外提供 OwnedModelUsage，连接无工具模型调用与现有耐久预算预留。

输入为 sessionmaker、Loop/Round、Patrol attempt 或 Worker request 及唯一领取身份；输出为逐请求预留和幂等实际消费。
具体工作流为验证类型化来源及当前控制版本，复用 LoopIndexBudgetReservation 和 LoopUsageLedger，
每次采样前单独短事务准入，取消后保留未报告预留；不另建账本，不在模型执行期间持有事务。
示例：model.bind_usage_receipts(OwnedModelUsage(sessions, loop_id, round_id, "worker", request_id, retry_identity=request.retry_identity))。
"""

from sqlalchemy import select

from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopPatrolAttempt, LoopWorkerRequest, LoopRound
from backend.app.desktop.agent_loop.context_expansion.index_budget_repository import IndexBudgetReservationRepository
from backend.app.desktop.agent_loop.usage import LoopUsageDelta


class OwnedModelUsage:
    def __init__(self, sessions, loop_id, round_id, kind, source_id, *, retry_identity=None):
        self._sessions = sessions
        self._loop_id = loop_id
        self._round_id = round_id
        self._kind = kind
        self._source_id = source_id
        self._retry_identity = retry_identity

    async def reserve(self, input_tokens, output_tokens):
        async with self._sessions.begin() as session:
            loop = await session.get(AgentLoop, self._loop_id, with_for_update=True)
            model = {"patrol": LoopPatrolAttempt, "worker": LoopWorkerRequest, "round": LoopRound}.get(self._kind)
            source = await session.get(model, self._source_id) if model else None
            if (loop is None or loop.status != "running" or loop.current_round_id != self._round_id
                or source is None or (source.loop_id, source.round_id) != (self._loop_id, self._round_id)
                or (self._kind != "round" and source.status != "running")
                or (self._kind == "round" and (not source.observation_id or source.status in {"settled", "error", "superseded"}))):
                raise ValueError("模型消费来源已取消或无效")
            grant = await session.scalar(select(LoopDelegationGrant).where(
                LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == loop.authority_revision,
                LoopDelegationGrant.status == "active"))
            if grant is None:
                raise ValueError("模型消费授权不存在")
            if self._kind == "worker" and (not self._retry_identity or source.retry_identity != self._retry_identity):
                raise ValueError("模型消费 Worker attempt 已过期")
            revision, limits = grant.revision, dict(grant.budgets)
        repository = IndexBudgetReservationRepository(self._sessions, self._loop_id, revision, limits, {},
            owner={"owner_kind": self._kind, "owner_id": self._source_id, "round_id": self._round_id,
                   **({"retry_identity": self._retry_identity} if self._kind == "worker" else {})})
        await repository.reserve(input_tokens, output_tokens)
        return repository

    @staticmethod
    async def settle(receipt, measured, reported):
        if reported:
            await receipt.settle(LoopUsageDelta.from_model_usage(measured))
        else:
            await receipt.mark_unknown()
