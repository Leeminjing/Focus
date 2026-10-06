"""本文件对外提供 WorkerAttemptAuthority 与 WorkerAttemptRejected。

输入为事务、持久 Worker 和领取身份；输出为已锁定且获当前授权的 Loop/Worker，或明确过期错误。
具体工作流为按 Loop→Worker 顺序加锁，核对 Round、控制/Mission 版本及唯一 retry_identity；
领取时产生唯一身份，写回时追加该尝试历史，模型消费与结果提交共用资格检查。
示例：loop, worker = await WorkerAttemptAuthority().require_current(session, request)。
"""

from datetime import UTC, datetime
import uuid

from sqlalchemy import select

from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopRound, LoopWorkerRequest


class WorkerAttemptRejected(ValueError):
    pass


class WorkerAttemptAuthority:
    async def require_current(self, session, request, *, statuses=("running",)):
        loop = await session.get(AgentLoop, request.loop_id, with_for_update=True, populate_existing=True)
        row = await session.get(LoopWorkerRequest, request.worker_request_id, with_for_update=True, populate_existing=True)
        if (row is None or loop is None or row.status not in statuses
            or not request.retry_identity or row.retry_identity != request.retry_identity
            or (row.loop_id, row.round_id, row.kind) != (request.loop_id, request.round_id, request.kind)):
            raise WorkerAttemptRejected("Worker attempt 已过期")
        await self.require_authorized(session, loop, row)
        return loop, row

    async def require_authorized(self, session, loop, row):
        round_row = await session.get(LoopRound, row.round_id)
        if (loop.status != "running" or loop.current_round_id != row.round_id
            or round_row is None or round_row.loop_id != loop.loop_id
            or round_row.status in {"settled", "superseded", "error"}
            or (round_row.authority_revision, round_row.goal_revision) != (loop.authority_revision, loop.goal_revision)
            or row.scope.get("execution_authority_revision", loop.authority_revision) != loop.authority_revision
            or row.scope.get("execution_goal_revision", loop.goal_revision) != loop.goal_revision):
            raise WorkerAttemptRejected("Worker Round 或控制身份已过期")
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id,
            LoopDelegationGrant.revision == loop.authority_revision,
            LoopDelegationGrant.status == "active"))
        if grant is None or (grant.expires_at is not None and grant.expires_at <= datetime.now(UTC)):
            raise WorkerAttemptRejected("Worker delegation 已撤销或过期")
        return grant

    @staticmethod
    def claim(loop, row):
        row.status = "running"
        row.retry_identity = f"worker:{row.worker_request_id}:attempt:{row.attempt}:{uuid.uuid4().hex}"
        row.scope = {**row.scope, "execution_authority_revision": loop.authority_revision,
                     "execution_goal_revision": loop.goal_revision}

    @staticmethod
    def archive(row, status, result):
        history = list(row.scope.get("attempt_history") or ())
        history.append({"attempt": row.attempt, "retry_identity": row.retry_identity,
                        "status": status, "result": result})
        row.scope = {**row.scope, "attempt_history": history}
