"""本文件对外提供 ProgressWorkLease，维持一次 frozen work 的有效领取。

输入为独立 session 工厂、执行政策、Observation identity、fence 和 coroutine 工厂；输出为该工作结果或明确失去租约错误。
具体工作流为在短事务重查并续租当前有效 fence，事务外并发执行与续租；续租失败、外部取消或请求异常会取消并等待全部任务清理。
本模块不调用模型、不修改输入、不发布进度、不启动执行；示例：await lease.run(observation_id, fence, consolidate)。
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from backend.app.desktop.agent_loop.task_progress.models import LoopProgressWork
from backend.app.desktop.agent_loop.task_progress.repository import ProgressPublicationRejected


class ProgressWorkLease:
    def __init__(self, sessions, policy):
        self._sessions = sessions
        self._policy = policy

    async def run(self, observation_id, fence, operation):
        await self._renew(observation_id, fence)
        worker = asyncio.create_task(operation())
        keeper = asyncio.create_task(self._keep(observation_id, fence))
        try:
            done, _ = await asyncio.wait((worker, keeper), return_when=asyncio.FIRST_COMPLETED)
            if worker in done:
                return await worker
            await keeper
            raise ProgressPublicationRejected("任务记忆续租提前结束")
        finally:
            for task in (worker, keeper):
                if not task.done():
                    task.cancel()
            await asyncio.gather(worker, keeper, return_exceptions=True)

    async def _keep(self, observation_id, fence):
        while True:
            await asyncio.sleep(self._policy.renew_seconds)
            await self._renew(observation_id, fence)

    async def _renew(self, observation_id, fence):
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            work = await session.scalar(select(LoopProgressWork).where(
                LoopProgressWork.observation_id == observation_id, LoopProgressWork.fence == fence,
                LoopProgressWork.state == "claimed", LoopProgressWork.lease_expires_at > now
            ).with_for_update())
            if work is None:
                raise ProgressPublicationRejected("任务记忆 worker fence/lease 已失效，停止请求")
            work.lease_expires_at = now + timedelta(seconds=self._policy.lease_seconds)
