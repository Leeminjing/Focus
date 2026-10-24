"""本文件对外提供 ExecutionCancellation 的精确取消验证与耐久提交。

输入为 Kernel 事务、当前冻结 Observation 和包含所属 Round/目标 identity 的 action；输出为
已终止或请求取消的审计结果。工作流为验证目标在本 Loop 冻结后果目录中且来源完全匹配，
终止目标的执行资格并追加既有 outbox；执行器接到通知后清理，Run.settled_at 仅由 finalizer 写入。
示例：await ExecutionCancellation().apply(db, loop, observation, action, action_id)。
"""

from datetime import UTC, datetime
import uuid

from sqlalchemy import func, select

from backend.app.desktop.patrol.agent_loop.models import LoopDirective, LoopEventOutbox, LoopWorkerRequest
from backend.app.desktop.patrol.agent_loop.execution.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.persistence.models import DesktopRun


class ExecutionCancellation:
    async def validate(self, session, loop, observation, action):
        model = {"run": DesktopRun, "directive": LoopDirective, "worker": LoopWorkerRequest}[action.target_kind]
        row = await session.get(model, action.target_id, with_for_update=True)
        if row is None or (row.loop_id, row.round_id) != (loop.loop_id, action.round_id):
            raise ValueError("取消目标 Loop/Round identity 不匹配")
        if observation is None or not await self._frozen_target(session, loop.loop_id, observation.envelope, action):
            raise ValueError("取消目标不属于冻结 Observation")
        return row

    async def apply(self, session, loop, observation, action, action_id):
        row = await self.validate(session, loop, observation, action)
        run_ids, worker_ids = [], []
        if action.target_kind == "directive":
            if row.lifecycle_state in {"settled", "failed", "cancelled", "rejected", "delivery_failed"}:
                return {"status": "already_terminal", "target_id": action.target_id}
            await DirectiveLifecycleRepository().transition(session, row.directive_id, "cancelled", reason=action.reason)
            row.status = "cancelled"
            if row.launched_run_id:
                run_ids.append(row.launched_run_id)
        elif action.target_kind == "run":
            if row.settled_at is not None:
                return {"status": "already_terminal", "target_id": action.target_id}
            run_ids.append(row.run_id)
        else:
            if row.status not in {"pending", "running"}:
                return {"status": "already_terminal", "target_id": action.target_id}
            if row.status == "pending":
                row.completed_at = datetime.now(UTC)
            row.status = "cancelled"
            row.result = {**(row.result or {}), "reason": action.reason}
            worker_ids.append(row.worker_request_id)
        for run_id in run_ids:
            run = await session.get(DesktopRun, run_id, with_for_update=True)
            if run and run.settled_at is None:
                run.status, run.error = "interrupted", action.reason
                if run.directive_id:
                    directive = await session.get(LoopDirective, run.directive_id, with_for_update=True)
                    if directive and directive.lifecycle_state not in {"settled", "failed", "cancelled", "rejected", "delivery_failed"}:
                        await DirectiveLifecycleRepository().transition(session, directive.directive_id, "cancelled", reason=action.reason)
        sequence = int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(
            LoopEventOutbox.loop_id == loop.loop_id)) or 0) + 1
        session.add(LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop.loop_id, sequence=sequence,
            event_type="loop.execution.cancel_requested", idempotency_key=f"execution-cancel:{action_id}",
            payload={"round_id": action.round_id, "action_id": action_id,
                "run_ids": run_ids, "worker_request_ids": worker_ids, "reason": action.reason}))
        return {"status": "cancel_requested", "target_id": action.target_id, "round_id": action.round_id}

    @classmethod
    async def _frozen_target(cls, session, loop_id, envelope, action):
        if cls._contains_target(envelope.get("intervention") or {}, action):
            return True
        from backend.app.desktop.patrol.agent_loop.projections.causal_snapshot import read_causal_snapshot

        for entry in envelope.get("causal_chain", ()):
            if entry.get("availability") == "legacy_unknown":
                continue
            state = await read_causal_snapshot(session, loop_id, entry)
            if cls._contains_target(state.get("intervention") or {}, action):
                return True
        return False

    @staticmethod
    def _contains_target(snapshot, action):
        collection, key = {"run": ("runs", "run_id"), "directive": ("directives", "directive_id"),
                           "worker": ("workers", "worker_request_id")}[action.target_kind]
        return any(row.get(key) == action.target_id and row.get("round_id") == action.round_id
                   for row in snapshot.get(collection, ()))
