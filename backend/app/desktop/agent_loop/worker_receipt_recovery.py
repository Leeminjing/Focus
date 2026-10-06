"""本文件对外提供WorkerReceiptRecovery的未报告消费恢复。
输入为恢复事务与持久Worker owner回执；输出为终结或旧领取身份回执的显式unknown分类。
具体工作流为按Loop→Worker加锁刷新，核对原loop/round/领取身份，再复用原回执仓库分类；当前运行中的同身份请求保留。
不按时间猜消费、不释放预算、不结算Token、不变更Worker或Loop状态；迟到实际报告仍可幂等替换unknown。
示例：await WorkerReceiptRecovery().reconcile(session)；不触及无可信Worker归属或已明确unknown的历史记录。
"""

from sqlalchemy import select

from backend.app.desktop.agent_loop.context_expansion.index_budget_repository import IndexBudgetReservationRepository
from backend.app.desktop.agent_loop.context_expansion.models import LoopIndexBudgetReservation
from backend.app.desktop.agent_loop.models import AgentLoop, LoopWorkerRequest


class WorkerReceiptRecovery:
    async def reconcile(self, session):
        receipts = tuple((await session.scalars(select(LoopIndexBudgetReservation).where(
            LoopIndexBudgetReservation.settled_at.is_(None),
            LoopIndexBudgetReservation.actual_usage['owner_kind'].astext == 'worker',
            LoopIndexBudgetReservation.actual_usage['receipt_state'].astext.is_distinct_from('unknown'),
        ).order_by(LoopIndexBudgetReservation.loop_id, LoopIndexBudgetReservation.reservation_id))).all())
        changed = 0
        for receipt in receipts:
            owner = receipt.actual_usage or {}
            loop = await session.get(AgentLoop, receipt.loop_id, with_for_update=True, populate_existing=True)
            worker = await session.get(LoopWorkerRequest, owner.get('owner_id'), with_for_update=True, populate_existing=True)
            if loop is None or worker is None or not owner.get('retry_identity'):
                continue
            if (worker.loop_id, worker.round_id) != (receipt.loop_id, owner.get('round_id')):
                continue
            if worker.status == 'running' and worker.retry_identity == owner['retry_identity']:
                continue
            await IndexBudgetReservationRepository.from_receipt(None, receipt).mark_unknown_in(session)
            changed += 1
        return changed
