"""本文件对外提供WorkerModelUsage，协调Worker结果与已有模型消费回执。
输入为sessionmaker及已领取Worker；输出为原owner预留、及时实际消费或结果事务内的同receipt补交。
具体工作流为复用OwnedModelUsage；独立回执事务失败时保留实际测量，提交结果、失败或取消时在调用方事务补交。
无真实报告时仅标unknown并保留预算；补交失败使事务回滚，不重复采样、不修改历史或控制状态。
示例：receipts = WorkerModelUsage(sessions, request)；模型绑定后在结果事务await receipts.flush_pending(session)。
"""

import logging

from sqlalchemy.exc import SQLAlchemyError

from backend.app.desktop.agent_loop.model_usage_owner import OwnedModelUsage
from backend.app.desktop.agent_loop.usage import LoopUsageDelta

logger = logging.getLogger(__name__)


class WorkerModelUsage(OwnedModelUsage):
    def __init__(self, sessions, request):
        super().__init__(sessions, request.loop_id, request.round_id, 'worker', request.worker_request_id,
                         retry_identity=request.retry_identity)
        self._pending = {}

    @property
    def has_pending(self):
        return bool(self._pending)

    async def settle(self, receipt, measured, reported):
        try:
            await super().settle(receipt, measured, reported)
        except (SQLAlchemyError, OSError) as exc:
            self._pending[receipt] = (LoopUsageDelta.from_model_usage(measured), reported)
            logger.warning('Worker model usage awaits result transaction: %s', type(exc).__name__)

    async def flush_pending(self, session):
        for receipt, (delta, reported) in tuple(self._pending.items()):
            if reported:
                await receipt.settle_in(session, delta)
            else:
                await receipt.mark_unknown_in(session)
