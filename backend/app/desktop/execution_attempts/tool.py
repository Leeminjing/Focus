"""本文件对外提供 ToolExecutionLedger 的工具执行意图与结果保存端口。

输入为稳定调用 identity、Run 与参数、注入的实时控制校验及 canonical ToolMessage；输出为新 claim、可靠缓存结果或不确定诊断。
具体工作流为同一短事务先锁控制版本并校验 Run，再提交唯一执行 claim；暂停后的新工具意图拒绝，已开始的调用仍保存结果。
已有意图但无结果时拒绝恢复重复副作用。Windows Shell 将未确认清理的 attempt identity 保存在 Run.workspace_result；只有服务端逐调用回执中的 confirmed/not_started 可解除，控制失败或回执丢失保留屏障并禁止继续工具执行。本模块通过组合根注入控制政策，文件结果提交遵循 Loop→Run 的既有锁序。
示例：cached = await ledger.claim(identity, run_id, call)；await ledger.complete(identity, result)。
每次工具调用可准备独立文件采集端口；成功工具结果、不可变文件版本与既有 Run outbox 在同一事务提交，失败提交保留原执行不确定性。
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from backend.app.desktop.models import DesktopRun, ToolExecutionAttempt
from focus.history import content_hash, deserialize_history_message, serialize_history_message
from focus.runtime.tool_attempts import ToolExecutionUncertain
from focus.sandbox.recovery import ShellRecoveryEvidence


class ToolExecutionLedger:
    def __init__(self, sessions, *, authority_validator=None, secret_provider=None, shell_evidence=None):
        self._sessions = sessions
        self._authority_validator = authority_validator
        self._secret_provider = secret_provider or (lambda: ())
        self._shell_evidence = shell_evidence or ShellRecoveryEvidence()

    async def shell_receipt(self, identity):
        async with self._sessions() as session:
            row = await session.get(ToolExecutionAttempt, identity)
            run = await session.get(DesktopRun, row.run_id)
            evidence = run.workspace_result["shell_cleanup"][identity]["execution"]
            return {"process_cleanup": "not_started", "execution": evidence,
                    "status_path": str(self._shell_evidence.path(evidence))}

    async def prepare_file_capture(self, run_id):
        from backend.app.desktop.domain_evidence.file_versions import FileVersionCapture
        from backend.app.desktop.workspace_coordination.models import WorkspaceSlot

        async with self._sessions() as session:
            run = await session.get(DesktopRun, run_id)
            slot_id = (run.workspace_anchor or {}).get("slot_id") if run else None
            slot = await session.get(WorkspaceSlot, slot_id) if slot_id else None
            return FileVersionCapture(slot_id, slot.root_path, self._secret_provider()) if slot else None

    async def claim(self, identity, run_id, call, *, requires_process_cleanup=False):
        digest = content_hash(call)
        evidence = await asyncio.to_thread(self._shell_evidence.prepare, run_id, identity, call["id"]) if requires_process_cleanup else None
        async with self._sessions() as session:
            run = await session.get(DesktopRun, run_id)
            if run is None:
                raise ToolExecutionUncertain("工具调用缺少持久 Run")
            if run.loop_id and run.round_id:
                if self._authority_validator is None:
                    raise ToolExecutionUncertain("Loop 工具调用缺少实时控制校验")
                await self._authority_validator(session, run)
            run = await session.get(DesktopRun, run_id, with_for_update=True, populate_existing=True)
            pending = dict((run.workspace_result or {}).get("shell_cleanup") or {})
            if pending and (run.status not in {"pending", "running"} or any(item.get("state") == "unknown" for item in pending.values())):
                raise ToolExecutionUncertain("Shell 进程树清理尚未确认；请核对控制端与后代进程，禁止继续执行或自动重试")
            created = await session.scalar(insert(ToolExecutionAttempt).values(
                attempt_id=identity, run_id=run_id, call_id=call["id"], call_hash=digest, status="claimed"
            ).on_conflict_do_nothing().returning(ToolExecutionAttempt.attempt_id))
            if created:
                if requires_process_cleanup:
                    pending[identity] = {"call_id": call["id"], "tool": call["name"], "state": "pending", "execution": evidence}
                    run.workspace_result = {**(run.workspace_result or {}), "shell_cleanup": pending}
                await session.commit()
                return None
            row = await session.scalar(select(ToolExecutionAttempt).where(ToolExecutionAttempt.attempt_id == identity).with_for_update())
            if row.call_hash != digest:
                raise ToolExecutionUncertain("工具 identity 参数冲突，禁止重试")
            if row.status != "completed" or row.result is None:
                raise ToolExecutionUncertain(f"工具 {call['name']} 已有执行意图但结果不确定；请核对副作用后通过新调用明确重试")
            return deserialize_history_message(row.result)

    async def complete(self, identity, result, *, process_cleanup=None, file_capture=None):
        cleanup_unknown = False
        async with self._sessions() as session:
            existing = await session.get(ToolExecutionAttempt, identity)
            if existing is None:
                raise RuntimeError("工具结果缺少唯一执行 claim")
            if file_capture is not None:
                from backend.app.desktop.agent_loop.models import AgentLoop

                observed_run = await session.get(DesktopRun, existing.run_id)
                if observed_run.loop_id:
                    await session.get(AgentLoop, observed_run.loop_id, with_for_update={"read": True, "key_share": True})
            run = await session.get(DesktopRun, existing.run_id, with_for_update=True, populate_existing=True)
            row = await session.scalar(select(ToolExecutionAttempt).where(ToolExecutionAttempt.attempt_id == identity).with_for_update())
            if row is None or row.status != "claimed":
                raise RuntimeError("工具结果缺少唯一执行 claim")
            if result is not None:
                row.status, row.result = "completed", serialize_history_message(result)
            pending = dict((run.workspace_result or {}).get("shell_cleanup") or {})
            if identity in pending:
                if process_cleanup in {"confirmed", "not_started"}:
                    pending.pop(identity)
                else:
                    pending[identity] = {**pending[identity], "state": "unknown"}
                    run.status = "interrupted"
                    run.error = "Shell 进程树清理未确认；已保留 Run 与工作区，需核对实际进程退出证据"
                    cleanup_unknown = True
                run.workspace_result = {**(run.workspace_result or {}), "shell_cleanup": pending}
            if file_capture is not None and result is not None and result.status == "success" and not cleanup_unknown:
                from backend.app.desktop.domain_evidence.file_versions import FileVersionRecorder

                await FileVersionRecorder().record(session, run, row, file_capture)
            await session.commit()
        if cleanup_unknown:
            raise ToolExecutionUncertain("Shell 进程树清理未确认；Run 不可结算，也不可自动重放")
