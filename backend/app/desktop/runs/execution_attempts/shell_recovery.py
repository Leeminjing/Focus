"""本文件对外提供 ShellCleanupRecovery，列出受阻 Run 并核验退出证据。

输入为 Run identity；输出为受阻目录清单、幂等结算结果或证据不足诊断。
具体工作流为服务端读取预登记回执或本机重启证据，全部 attempt 核验后交给 finalizer 原子结算；永不重放工具。
旧版本缺少执行证据的 Run 先登记本机启动身份，下一次 Windows 重启后可完成核验。
示例：settlement = await recovery.recover(run_id)。
"""

import asyncio

from sqlalchemy import select

from backend.app.desktop.persistence.models import DesktopRun, DesktopThread
from backend.app.desktop.workspaces.coordination.models import WorkspaceSlot
from focus.sandbox.recovery import ShellRecoveryEvidence, windows_boot


class ShellCleanupRecovery:
    def __init__(self, sessions, finalizer, run_manager, *, evidence=None, boot_provider=windows_boot):
        self._sessions = sessions
        self._finalizer = finalizer
        self._runs = run_manager
        self._evidence = evidence or ShellRecoveryEvidence()
        self._boot = boot_provider

    async def blocked(self) -> list[dict]:
        async with self._sessions() as session:
            rows = await session.scalars(select(DesktopRun).where(
                DesktopRun.settled_at.is_(None),
                DesktopRun.workspace_result["shell_cleanup"].astext.not_in(("{}", "null")),
            ).order_by(DesktopRun.created_at, DesktopRun.run_id))
            result = []
            for run in rows:
                thread = await session.get(DesktopThread, run.task_id)
                slot_id = (run.workspace_anchor or {}).get("slot_id")
                slot = await session.get(WorkspaceSlot, slot_id) if slot_id else None
                result.append({"run_id": run.run_id, "task_id": run.task_id,
                    "title": thread.title if thread else run.task_id, "status": run.status,
                    "directory": slot.root_path if slot else (run.workspace_anchor or {}).get("workspace_path"),
                    "attempts": [{"attempt_id": identity, "call_id": item.get("call_id"),
                        "tool": item.get("tool"), "state": item.get("state")}
                        for identity, item in run.workspace_result["shell_cleanup"].items()]})
            return result

    async def recover(self, run_id: str):
        record = self._runs.get(run_id)
        if record and (record.status.value in {"pending", "running"} or (record.task and not record.task.done())):
            raise ValueError("Run 执行器仍在运行，请先停止执行后核验")
        current_boot = await asyncio.to_thread(self._boot)
        enrolled = False
        async with self._sessions.begin() as session:
            run = await session.get(DesktopRun, run_id, with_for_update=True)
            if run is None:
                raise LookupError(f"Run 不存在: {run_id}")
            if run.settled_at is not None:
                return await self._finalizer._settlement(session, run, idempotent=True)
            if run.status != "interrupted":
                raise ValueError("Run 尚未中断，请先停止执行后核验")
            pending = dict((run.workspace_result or {}).get("shell_cleanup") or {})
            if not pending:
                raise ValueError("Run 没有待核验的 Shell 清理")
            for identity, item in pending.items():
                if not item.get("execution"):
                    if current_boot is None:
                        raise ValueError("Windows 系统启动信息不可用，无法登记旧执行的退出核验基线")
                    execution = await asyncio.to_thread(self._evidence.prepare, run_id, identity, item["call_id"])
                    execution["boot"] = current_boot
                    pending[identity] = {**item, "execution": execution}
                    enrolled = True
                elif not item["execution"].get("boot") and not item.get("recovery_boot") and current_boot:
                    pending[identity] = {**item, "recovery_boot": current_boot}
            run.workspace_result = {**run.workspace_result, "shell_cleanup": pending}
        if enrolled:
            raise ValueError("旧执行缺少关联回执；已登记本机启动证据，请重启 Windows 后再次核验")
        proofs = {}
        for identity, item in pending.items():
            evidence = item["execution"]
            if evidence.get("run_id") != run_id or evidence.get("attempt_id") != identity or evidence.get("call_id") != item.get("call_id"):
                raise ValueError("Shell 回执关联身份不匹配，不能恢复")
            proof = await asyncio.to_thread(self._evidence.verify, evidence, current_boot=current_boot,
                reboot_baseline=item.get("recovery_boot"))
            proofs[identity] = {"execution": evidence, **proof}
        return await self._finalizer.recover_shell_cleanup(run_id, pending, proofs)
