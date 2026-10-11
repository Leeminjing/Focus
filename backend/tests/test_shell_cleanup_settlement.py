"""本文件对外提供 Shell 清理回执与 Run 结算的持久边界验收。

输入为真实工具账本、PostgreSQL Run/派发/工作区租约及明确或缺失的后端清理回执；输出为未知不结算、不重放、不复用目录和已确认正常收口的断言。
具体工作流通过生产中间件执行替换的后端故障点，再运行 finalizer、重启恢复和租约获取；另在 claim 后模拟进程丢失，验证持久 pending 不能被重启当作退出。
示例：python -m pytest backend/tests/test_shell_cleanup_settlement.py -q。
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import os
import threading
import uuid

import pytest
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.execution_attempts.tool import ToolExecutionLedger
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.run_orchestration.dispatch import RunDispatchRecovery
from backend.app.desktop.run_orchestration.lifecycle import RunLifecycleFinalizer
from backend.app.desktop.run_orchestration.models import RunDispatch, RunOutboxEvent
from backend.app.desktop.workspace_coordination.leases import WorkspaceLeaseConflict, WorkspaceLeaseManager
from backend.app.desktop.workspace_coordination.lifecycle import WorkspaceSlotLifecycle
from backend.app.desktop.workspace_coordination.models import WorkspaceLease, WorkspaceSlot
from backend.app.desktop.workspace_coordination.schemas import WorkspaceLeaseRequest
from backend.tests.test_execution_attempt_audit import _seed
from focus.runtime.runs.manager import RunRecord
from focus.runtime.runs.schemas import DisconnectMode, RunStatus
from focus.runtime.tool_attempts import ToolExecutionMiddleware, ToolExecutionUncertain
from focus.sandbox.contracts import SandboxUnavailable, ShellExecutionResult
from focus.security.middleware import AccessPolicyMiddleware
from focus.tools.builtins import workspace_tools

pytestmark = pytest.mark.usefixtures("isolated_postgres_database")


class _Checkpointer:
    async def aget_tuple(self, config):
        return None


async def _execution(sessions, path):
    run_id, security = await _seed(sessions, path)
    slot_id = uuid.uuid4().hex
    async with sessions.begin() as session:
        session.add(WorkspaceSlot(slot_id=slot_id, workspace_id=run_id, kind="isolated",
            root_path=str(path), current_fingerprint="a" * 64))
        session.add(RunDispatch(dispatch_id=uuid.uuid4().hex, run_id=run_id, status="running",
            claimed_by="shell-test", attempt=1, fencing_token=1, lease_expires_at=datetime.now(UTC) + timedelta(minutes=1)))
    leases = WorkspaceLeaseManager(sessions)
    lease = await leases.acquire(WorkspaceLeaseRequest(slot_id=slot_id, run_id=run_id, mode="write"))
    async with sessions.begin() as session:
        run = await session.get(DesktopRun, run_id)
        run.workspace_anchor = {"slot_id": slot_id, "lease_id": lease.lease_id, "mode": "write"}
    record = RunRecord(run_id=run_id, thread_id=run_id, status=RunStatus.interrupted, on_disconnect=DisconnectMode.cancel)
    security = replace(security, authorization=replace(security.authorization, permissions=("host_command",)))
    return run_id, slot_id, lease, record, security


async def _assert_blocked(sessions, run_id, slot_id, lease, record):
    finalizer = RunLifecycleFinalizer(sessions, _Checkpointer())
    with pytest.raises(RuntimeError, match="清理未确认"):
        await finalizer.finalize(record)
    assert not await finalizer.abort_prepared(run_id, "control failed")
    await RunDispatchRecovery(sessions).reconcile()
    assert await finalizer.reconcile_active("process restarted") == 0
    async with sessions.begin() as session:
        run = await session.get(DesktopRun, run_id)
        dispatch = await session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id))
        held = await session.get(WorkspaceLease, lease.lease_id)
        assert run.settled_at is None and run.workspace_result["shell_cleanup"]
        assert dispatch.settled_at is None
        assert held.status == "active" and held.released_at is None
        assert not list(await session.scalars(select(RunOutboxEvent).where(RunOutboxEvent.run_id == run_id)))
        held.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(WorkspaceLeaseConflict, match="清理未确认"):
        await WorkspaceLeaseManager(sessions).acquire(WorkspaceLeaseRequest(slot_id=slot_id, run_id=uuid.uuid4().hex, mode="write"))
    assert not await WorkspaceSlotLifecycle(sessions).cleanup(slot_id, "forced cleanup", force=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows Shell 回执中间件")
@pytest.mark.parametrize("outcome", ["unknown", "confirmed", "not_started", "validation_error"])
def test_shell_backend_receipt_controls_durable_settlement(tmp_path, monkeypatch, outcome):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            run_id, slot_id, lease, record, security = await _execution(sessions, tmp_path)
            class Backend:
                def run(self, request):
                    if outcome == "not_started":
                        raise SandboxUnavailable("backend unavailable before process launch")
                    return ShellExecutionResult(request.binding.mode, request.binding.workspace, True, "partial",
                        "", "control failure", None, "control_failed", process_cleanup=outcome)
            monkeypatch.setattr(workspace_tools, "_SHELL_BACKEND", Backend())
            ledger = ToolExecutionLedger(sessions)
            security = replace(security, extras={**security.extras, "tool_execution_ledger": ledger})
            call = {"name": "powershell", "args": {"command": "echo managed"}, "id": "shell-call", "type": "tool_call"}
            runtime = ToolRuntime(state={"messages": [AIMessage(content="", id="parent", tool_calls=[call])]},
                context=security.to_runtime_context(), config={}, stream_writer=None, tool_call_id=call["id"], store=None, tools=[])
            request = ToolCallRequest(tool_call=call, tool=workspace_tools.powershell, state={}, runtime=runtime)
            async def handler(bound):
                command = "" if outcome == "validation_error" else call["args"]["command"]
                content = await asyncio.to_thread(workspace_tools.powershell.func, command=command, runtime=bound.runtime)
                return ToolMessage(content=content, tool_call_id=call["id"])
            if outcome == "unknown":
                with pytest.raises(ToolExecutionUncertain, match="清理未确认"):
                    await ToolExecutionMiddleware().awrap_tool_call(request, handler)
                await _assert_blocked(sessions, run_id, slot_id, lease, record)
                with pytest.raises(ToolExecutionUncertain, match="清理尚未确认"):
                    await ledger.claim("new-tool", run_id, {"id": "new", "name": "read_file", "args": {}})
            else:
                if outcome == "validation_error":
                    with pytest.raises(ValueError, match="命令不能为空"):
                        await ToolExecutionMiddleware().awrap_tool_call(request, handler)
                else:
                    await ToolExecutionMiddleware().awrap_tool_call(request, handler)
                async with sessions() as session:
                    assert not (await session.get(DesktopRun, run_id)).workspace_result["shell_cleanup"]
                await RunLifecycleFinalizer(sessions, _Checkpointer()).finalize(record)
                async with sessions() as session:
                    assert (await session.get(DesktopRun, run_id)).settled_at is not None
                    assert (await session.get(WorkspaceLease, lease.lease_id)).status == "released"
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_process_restart_after_shell_claim_preserves_cleanup_barrier(tmp_path):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            run_id, slot_id, lease, record, _ = await _execution(sessions, tmp_path)
            await ToolExecutionLedger(sessions).claim("pending-shell", run_id,
                {"id": "lost", "name": "powershell", "args": {}}, requires_process_cleanup=True)
            await _assert_blocked(sessions, run_id, slot_id, lease, record)
        finally:
            await engine.dispose()
    asyncio.run(exercise())


@pytest.mark.skipif(os.name != "nt", reason="Windows Shell 取消回执中间件")
@pytest.mark.parametrize("cleanup", ["confirmed", "unknown"])
def test_repeated_cancel_persists_cleanup_receipt_before_propagating(tmp_path, monkeypatch, cleanup):
    async def exercise():
        engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        entered, release = threading.Event(), threading.Event()
        task = None
        try:
            run_id, slot_id, lease, record, security = await _execution(sessions, tmp_path)
            class Backend:
                def run(self, request):
                    entered.set()
                    assert release.wait(10)
                    assert request.cancel_event.is_set()
                    return ShellExecutionResult(request.binding.mode, request.binding.workspace, True, "partial",
                        "", "", None, "cancelled", process_cleanup=cleanup)
            monkeypatch.setattr(workspace_tools, "_SHELL_BACKEND", Backend())
            ledger = ToolExecutionLedger(sessions)
            security = replace(security, extras={**security.extras, "tool_execution_ledger": ledger})
            call = {"name": "powershell", "args": {"command": "echo managed"}, "id": "cancel-shell", "type": "tool_call"}
            runtime = ToolRuntime(state={"messages": [AIMessage(content="", id="cancel-parent", tool_calls=[call])]},
                context=security.to_runtime_context(), config={}, stream_writer=None, tool_call_id=call["id"], store=None, tools=[])
            request = ToolCallRequest(tool_call=call, tool=workspace_tools.powershell, state={}, runtime=runtime)
            async def execute(bound):
                content = await asyncio.to_thread(workspace_tools.powershell.func, command=call["args"]["command"], runtime=bound.runtime)
                return ToolMessage(content=content, tool_call_id=call["id"])
            async def recorded(bound):
                return await ToolExecutionMiddleware().awrap_tool_call(bound, execute)
            task = asyncio.create_task(AccessPolicyMiddleware().awrap_tool_call(request, recorded))
            assert await asyncio.to_thread(entered.wait, 10)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            if cleanup == "unknown":
                await _assert_blocked(sessions, run_id, slot_id, lease, record)
            else:
                await RunLifecycleFinalizer(sessions, _Checkpointer()).finalize(record)
                async with sessions() as session:
                    assert (await session.get(DesktopRun, run_id)).settled_at is not None
        finally:
            release.set()
            if task is not None and not task.done():
                await asyncio.gather(task, return_exceptions=True)
            await engine.dispose()
    asyncio.run(exercise())
