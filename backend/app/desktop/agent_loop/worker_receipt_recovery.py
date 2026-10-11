"""本文件对外提供WorkerReceiptRecovery的未报告消费恢复。
输入为恢复事务与持久 Worker、Patrol 或 Round owner 回执；输出为终结、旧领取或重启前认知回执的显式 unknown 分类。
具体工作流为按 Loop→owner 加锁刷新，核对原 loop/round/观察版本与领取身份，再复用原回执仓库分类；运行期当前同身份请求保留，进程重启不推测未报告 Round 消费。
不按时间猜消费、不释放预算、不结算Token、不变更Worker或Loop状态；迟到实际报告仍可幂等替换unknown。
示例：await WorkerReceiptRecovery().reconcile(session)；不触及无可信Worker归属或已明确unknown的历史记录。
"""

from sqlalchemy import select

from backend.app.desktop.agent_loop.context_expansion.index_budget_repository import IndexBudgetReservationRepository
from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
from backend.app.desktop.agent_loop.models import AgentLoop, LoopCoordinatorFence, LoopWorkerRequest, LoopPatrolAttempt, LoopRound


class WorkerReceiptRecovery:
    async def reconcile(self, session, *, process_restarted=False):
        receipts = tuple((await session.scalars(select(LoopIndexBudgetReservation).where(
            LoopIndexBudgetReservation.settled_at.is_(None),
            LoopIndexBudgetReservation.actual_usage['owner_kind'].astext.in_(("worker", "patrol", "round")),
            LoopIndexBudgetReservation.actual_usage['receipt_state'].astext.is_distinct_from('unknown'),
        ).order_by(LoopIndexBudgetReservation.loop_id, LoopIndexBudgetReservation.reservation_id))).all())
        changed = 0
        for receipt in receipts:
            owner = receipt.actual_usage or {}
            loop = await session.get(AgentLoop, receipt.loop_id, with_for_update=True, populate_existing=True)
            kind = owner.get("owner_kind")
            model = {"worker": LoopWorkerRequest, "patrol": LoopPatrolAttempt, "round": LoopRound}[kind]
            worker = await session.get(model, owner.get('owner_id'), with_for_update=True, populate_existing=True)
            if loop is None or worker is None:
                continue
            if (worker.loop_id, worker.round_id) != (receipt.loop_id, owner.get('round_id')):
                continue
            round_row = worker if kind == "round" else await session.get(LoopRound, worker.round_id)
            exact_observation = bool(owner.get("observation_id") and round_row
                and worker.observation_id == owner["observation_id"] == round_row.observation_id)
            if kind == "worker" and exact_observation and worker.status == 'running' and worker.retry_identity == owner.get('retry_identity'):
                continue
            if kind == "patrol" and exact_observation and worker.status == "running":
                fence = await session.get(LoopCoordinatorFence, worker.round_id)
                if fence and fence.fencing_token == worker.fencing_token:
                    continue
            if kind == "round" and exact_observation and not process_restarted and worker.status in {"observed", "curated", "waiting_workers"}:
                fence = await session.get(LoopCoordinatorFence, worker.round_id)
                if owner.get("fencing_token") is None or (fence and fence.fencing_token == owner["fencing_token"]):
                    continue
            await IndexBudgetReservationRepository.from_receipt(None, receipt).mark_unknown_in(session)
            changed += 1
        return changed
