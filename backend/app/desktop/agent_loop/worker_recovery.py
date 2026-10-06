"""本文件对外提供 WorkerRecoveryRepository，恢复既有失败 Worker 及其准确等待请求。

输入为已锁定 Loop、用户 recovery_action 和现有 Worker；输出为原子重排队或拒绝、恢复等待计数。
具体工作流为核对当前 Round/授权/预算与精确 scope，保留冻结输入和全部消费，授予单调编号的三次有限批次；
重启核对 error 工作并经既有 wait 服务幂等开请求。只使用既有 WorkerRuntime，不创建 scheduler。
示例：await WorkerRecoveryRepository().retry(session, loop, wait_request)。
"""

from datetime import UTC, datetime

from sqlalchemy import select

from backend.app.desktop.agent_loop.accounting_query import LoopAccountingQuery
from backend.app.desktop.agent_loop.budgets import LoopBudgetGuard, configured_provider_count
from backend.app.desktop.agent_loop.curator_assignments import CuratorAssignmentRepository
from backend.app.desktop.agent_loop.models import AgentLoop, LoopBudgetUsage, LoopWorkerRequest
from backend.app.desktop.agent_loop.usage import LoopUsageDelta, LoopUsageLedger
from backend.app.desktop.agent_loop.wait_requests import LoopWaitRequestService, open_recovery_wait
from backend.app.desktop.agent_loop.worker_attempts import WorkerAttemptAuthority, WorkerAttemptRejected


class WorkerRecoveryRepository:
    async def retry(self, session, loop, request):
        row = await session.get(LoopWorkerRequest, request.scope.get("worker_request_id"), with_for_update=True)
        if (row is None or row.status != "error" or request.kind != "recovery_action"
            or request.round_id != loop.current_round_id
            or (row.loop_id, row.round_id, row.kind) != (loop.loop_id, request.round_id, request.scope.get("worker_kind"))):
            raise WorkerAttemptRejected("Worker retry scope 或失败状态不匹配")
        grant = await WorkerAttemptAuthority().require_authorized(session, loop, row)
        await self._require_budget(session, loop, grant)
        if not any(item.get("retry_identity") == row.retry_identity for item in row.scope.get("attempt_history") or ()):
            WorkerAttemptAuthority.archive(row, row.status, row.result)
        row.attempt += 1
        row.max_attempts = row.attempt + 2
        row.status = "pending"
        row.retry_identity = None
        row.completed_at = None
        row.result = {"retry_authorized_by": request.request_id}
        await CuratorAssignmentRepository().retry(session, row.worker_request_id)
        usage = await session.get(LoopBudgetUsage, loop.loop_id, with_for_update=True)
        LoopUsageLedger.apply(usage, LoopUsageDelta(retries=1))
        return row

    async def reconcile(self, session):
        candidates = tuple(await session.scalars(select(LoopWorkerRequest.worker_request_id).where(LoopWorkerRequest.status == "error")
            .order_by(LoopWorkerRequest.loop_id, LoopWorkerRequest.worker_request_id)))
        opened = 0
        for identity in candidates:
            source = await session.get(LoopWorkerRequest, identity)
            loop = await session.get(AgentLoop, source.loop_id, with_for_update=True)
            row = await session.get(LoopWorkerRequest, identity, with_for_update=True, populate_existing=True)
            if row.status != "error" or loop is None:
                continue
            try:
                await WorkerAttemptAuthority().require_authorized(session, loop, row)
            except WorkerAttemptRejected:
                continue
            if await LoopWaitRequestService.active(session, loop.loop_id, lock=True) is not None:
                continue
            await open_recovery_wait(session, loop, f"{row.kind} Worker 失败: {str(row.result.get('error') or '需要显式重试')[:1000]}",
                source="loop-worker", round_id=row.round_id,
                scope={"worker_request_id": row.worker_request_id, "worker_kind": row.kind})
            opened += 1
        return opened

    async def _require_budget(self, session, loop, grant):
        view = await LoopAccountingQuery().read(session, loop.loop_id)
        usage = await session.get(LoopBudgetUsage, loop.loop_id)
        if usage is None:
            raise WorkerAttemptRejected("Worker retry 缺少持久预算账本")
        values = {name: int(getattr(usage, name, 0) or 0) for name in ("rounds", "retries", "lanes", "no_progress_count")}
        values.update(view["consumption"]["occupied"])
        values["providers"] = configured_provider_count(loop.equipment or {})
        values["duration_seconds"] = max(0, int((datetime.now(UTC) - loop.created_at).total_seconds()))
        if LoopBudgetGuard().evaluate(values, grant.budgets, "worker_retry", {"model_calls": 1, "retries": 1}).status == "exhausted":
            raise WorkerAttemptRejected("Worker retry budget 已耗尽")
