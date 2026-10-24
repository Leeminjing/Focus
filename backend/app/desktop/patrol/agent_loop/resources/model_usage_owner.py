"""本文件对外提供 OwnedModelUsage，连接无工具模型调用与现有耐久预算预留。

输入为 sessionmaker、Loop/Round、Patrol attempt 或 Worker request 及唯一领取身份；输出为逐请求预留和幂等实际消费。
具体工作流为验证类型化来源及当前控制版本，复用 LoopIndexBudgetReservation 和 LoopUsageLedger，
每次采样前单独短事务准入，取消后保留未报告预留；不另建账本，不在模型执行期间持有事务。
示例：model.bind_usage_receipts(OwnedModelUsage(sessions, loop_id, round_id, "worker", request_id, retry_identity=request.retry_identity))。
"""

from sqlalchemy import select

from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopPatrolAttempt, LoopWorkerRequest, LoopRound
from backend.app.desktop.patrol.agent_loop.context_expansion.index_budget_repository import IndexBudgetReservationRepository
from backend.app.desktop.patrol.agent_loop.resources.usage import LoopUsageDelta


class OwnedModelUsage:
    def __init__(self, sessions, loop_id, round_id, kind, source_id, *, retry_identity=None, observation_id=None, fencing_token=None):
        self._sessions = sessions
        self._loop_id = loop_id
        self._round_id = round_id
        self._kind = kind
        self._source_id = source_id
        self._retry_identity = retry_identity
        self._observation_id = observation_id
        self._fencing_token = fencing_token

    async def reserve(self, input_tokens, output_tokens, *, retry=False):
        async with self._sessions.begin() as session:
            loop = await session.get(AgentLoop, self._loop_id, with_for_update=True)
            model = {"patrol": LoopPatrolAttempt, "worker": LoopWorkerRequest, "round": LoopRound}.get(self._kind)
            source = await session.get(model, self._source_id) if model else None
            if (loop is None or loop.status != "running"
                or source is None or (source.loop_id, source.round_id) != (self._loop_id, self._round_id)
                or (self._kind != "round" and source.status != "running")
                or (self._kind == "round" and (not source.observation_id or source.status in {"settled", "error", "superseded"}))):
                raise ValueError("模型消费来源已取消或无效")
            round_row = source if self._kind == "round" else await session.get(LoopRound, self._round_id)
            if (round_row is None or round_row.status in {"settled", "error", "superseded"}
                    or round_row.authority_revision != loop.authority_revision
                    or (self._kind in {"round", "patrol"} and round_row.decision_id is not None)):
                raise ValueError("模型消费所属 Round 已失效")
            observation_id = self._observation_id if self._kind == "round" else source.observation_id
            if observation_id != round_row.observation_id:
                raise ValueError("模型消费 Observation 已被替换")
            grant = await session.scalar(select(LoopDelegationGrant).where(
                LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == loop.authority_revision,
                LoopDelegationGrant.status == "active"))
            if grant is None:
                raise ValueError("模型消费授权不存在")
            if self._kind == "worker" and (not self._retry_identity or source.retry_identity != self._retry_identity):
                raise ValueError("模型消费 Worker attempt 已过期")
            if self._kind == "patrol" and source.observation_id:
                from backend.app.desktop.patrol.agent_loop.governance.ownership import LoopFencingGuard

                await LoopFencingGuard().validate_current(session, source.round_id, source.fencing_token)
            if self._kind == "round" and self._fencing_token is not None:
                from backend.app.desktop.patrol.agent_loop.governance.ownership import LoopFencingGuard

                await LoopFencingGuard().validate_current(session, source.round_id, int(self._fencing_token))
            revision, limits = grant.revision, dict(grant.budgets)
        repository = IndexBudgetReservationRepository(self._sessions, self._loop_id, revision, limits, {},
            owner={"owner_kind": self._kind, "owner_id": self._source_id, "round_id": self._round_id,
                   "observation_id": observation_id,
                   **({"fencing_token": int(self._fencing_token)} if self._fencing_token is not None else {}),
                   **({"retry_identity": self._retry_identity} if self._kind == "worker" else {})})
        await repository.reserve(input_tokens, output_tokens, retry=retry)
        return repository

    @staticmethod
    async def settle(receipt, measured, reported):
        if reported:
            await receipt.settle(LoopUsageDelta.from_model_usage(measured))
        else:
            await receipt.mark_unknown()
