"""非 Windows Shell 的取消、进程组清理和文件观察回归测试。

真实进程测试在 POSIX 运行；预取消测试也可在 Windows 验证原平台分支。
运行：python -m pytest backend/tests/test_posix_shell.py -q。
"""

import asyncio
from dataclasses import replace
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage

from backend.app.desktop.runs.evidence.file_versions import FileVersionCapture
from backend.tests.runtime_context_support import tool_runtime
from focus.security.context import security_context_of
from focus.security.middleware import AccessPolicyMiddleware
from focus.security.policy import AccessMode
from focus.runtime.tool_attempts import ToolExecutionMiddleware
from focus.sandbox.contracts import ShellExecutionRequest
from focus.sandbox.posix import PosixShellBackend
from focus.sandbox.recovery import ShellRecoveryEvidence
from focus.security.execution import bind_call_execution
from focus.tools.builtins import workspace_tools


def _runtime(path, *, mode=AccessMode.FULL, cancel=None):
    runtime = tool_runtime(agent_id="main", task_id="task", workspace=str(path),
        permissions=("host_command",), access_mode=mode)
    capture = FileVersionCapture("slot", path)
    # 保留真实前后字节采集，只固定观察路径，避免依赖测试宿主机的 Git 安装。
    capture._git_paths = lambda: {path / "before.txt", path / "late.txt"}
    receipt = {"process_cleanup": "not_started"}
    security = security_context_of(runtime.context)
    runtime = replace(runtime, tool_call_id="call", context={**runtime.context,
        **replace(security, extras={**security.extras, "sandbox_cancel_event": cancel,
            "shell_cleanup_receipt": receipt, "file_version_capture": capture}).to_runtime_context()})
    return runtime, receipt, capture


def _run(script, runtime):
    return json.loads(workspace_tools._run_shell(script, runtime, sys.executable, ["-c"]))


def test_precancelled_posix_shell_never_starts_or_writes(tmp_path, monkeypatch):
    # 只替换工具模块的平台选择，保持 pathlib 和 subprocess 的真实宿主行为。
    monkeypatch.setattr(workspace_tools, "os", SimpleNamespace(name="posix"))
    cancel = threading.Event()
    cancel.set()
    runtime, receipt, capture = _runtime(tmp_path, cancel=cancel)
    result = _run("from pathlib import Path; Path('late.txt').write_text('ran')", runtime)
    assert not (tmp_path / "late.txt").exists()
    assert result["status"] == "cancelled"
    assert result["process_cleanup"] == receipt["process_cleanup"] == "not_started"
    assert capture.changes == []


posix_only = pytest.mark.skipif(os.name != "posix", reason="真实 POSIX 进程组验收")


@posix_only
def test_success_updates_receipt_and_captures_actual_file_changes(tmp_path):
    runtime, receipt, capture = _runtime(tmp_path)
    result = _run("from pathlib import Path; Path('before.txt').write_text('written'); print('ok')", runtime)
    assert result["status"] == "exited" and result["exit_code"] == 0
    assert result["stdout"] == "ok\n"
    assert result["backend_applied"] is False
    assert result["process_cleanup"] == receipt["process_cleanup"] == "confirmed"
    assert [(c["path"], c["after"]["text"]) for c in capture.changes] == [("before.txt", "written")]


def _wait_for(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"目标未就绪: {path}")
        time.sleep(0.01)


def _child_script():
    return ("import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('ready').write_text('ready'); time.sleep(1.5); "
        "Path('late.txt').write_text('escaped')")


def _parent_script(*, exit_early=False):
    return ("import subprocess,sys,time; from pathlib import Path; "
        "Path('before.txt').write_text('before stop'); "
        f"subprocess.Popen([sys.executable,'-c',{_child_script()!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "\nwhile not Path('ready').exists(): time.sleep(0.01)\n"
        + ("print('parent exited', flush=True)" if exit_early else "time.sleep(30)"))


@posix_only
@pytest.mark.parametrize("termination", ["cancelled", "timeout", "exited"])
def test_waits_for_child_even_when_parent_exits_and_child_closes_pipes(tmp_path, monkeypatch, termination):
    backend = PosixShellBackend()
    class ShortTimeout:
        def run(self, request):
            return backend.run(replace(request, timeout_seconds=0.6 if termination == "timeout" else 5))
    monkeypatch.setattr(workspace_tools, "_POSIX_SHELL_BACKEND", ShortTimeout())
    cancel = threading.Event()
    runtime, receipt, capture = _runtime(tmp_path, cancel=cancel)
    def cancel_when_ready():
        _wait_for(tmp_path / "ready")
        cancel.set()
    controller = threading.Thread(target=cancel_when_ready) if termination == "cancelled" else None
    if controller:
        controller.start()
    try:
        result = _run(_parent_script(exit_early=termination == "exited"), runtime)
    finally:
        if controller:
            controller.join(timeout=6)
    assert result["status"] == termination, result
    assert result["process_cleanup"] == receipt["process_cleanup"] == "confirmed", result
    assert not (tmp_path / "late.txt").exists()
    assert [(c["path"], c["after"]["text"]) for c in capture.changes] == [("before.txt", "before stop")]
    # 子进程忽略 TERM；必须完成强制退出，返回后不能继续写文件。
    time.sleep(1.6)
    assert not (tmp_path / "late.txt").exists()


@posix_only
def test_repeated_async_cancel_waits_for_real_shell_and_file_capture(tmp_path):
    async def exercise():
        runtime, receipt, capture = _runtime(tmp_path)
        request = ToolCallRequest(tool_call={"name": "sh", "id": "call", "args": {"command": "managed"}},
            tool=workspace_tools.sh, runtime=runtime, state={})
        async def handler(bound):
            result = await asyncio.to_thread(_run, _parent_script(), bound.runtime)
            assert result["status"] == "cancelled"
            return ToolMessage(content=json.dumps(result), tool_call_id="call")
        task = asyncio.create_task(AccessPolicyMiddleware().awrap_tool_call(request, handler))
        await asyncio.to_thread(_wait_for, tmp_path / "ready")
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=6)
        assert receipt["process_cleanup"] == "confirmed"
        assert [c["path"] for c in capture.changes] == ["before.txt"]
        assert not (tmp_path / "late.txt").exists()
    asyncio.run(exercise())


@posix_only
@pytest.mark.parametrize("mode", [AccessMode.READ_ONLY, AccessMode.WORKSPACE_WRITE])
def test_restricted_mode_reports_platform_limit_without_running(tmp_path, mode):
    runtime, receipt, _ = _runtime(tmp_path, mode=mode)
    result = _run("from pathlib import Path; Path('late.txt').write_text('ran')", runtime)
    assert result["status"] == "SANDBOX_UNAVAILABLE"
    assert result["process_cleanup"] == receipt["process_cleanup"] == "not_started"
    assert "requested_mode" in result["stderr"] and "danger-full-access" in result["stderr"]
    assert not (tmp_path / "late.txt").exists()


def test_non_windows_shell_claims_cleanup_barrier_and_passes_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_tools, "os", SimpleNamespace(name="posix"))
    async def exercise():
        runtime, receipt, _ = _runtime(tmp_path)
        calls = []
        class Ledger:
            async def claim(self, identity, run_id, call, **options):
                assert options == {"requires_process_cleanup": True}
            async def shell_receipt(self, identity):
                return receipt
            async def complete(self, identity, result, **completion):
                calls.append(completion)
        security = security_context_of(runtime.context)
        context = {**runtime.context, **replace(security, extras={**security.extras,
            "tool_execution_ledger": Ledger()}).to_runtime_context()}
        call = {"name": "sh", "args": {"command": "managed"}, "id": "call"}
        runtime = replace(runtime, context=context,
            state={"messages": [AIMessage(content="", id="parent", tool_calls=[call])]})
        request = ToolCallRequest(tool_call=call, tool=workspace_tools.sh, runtime=runtime, state={})
        async def handler(bound):
            security_context_of(bound.runtime.context).extras["shell_cleanup_receipt"]["process_cleanup"] = "unknown"
            return ToolMessage(content="unconfirmed", tool_call_id="call")
        await ToolExecutionMiddleware().awrap_tool_call(request, handler)
        assert calls == [{"process_cleanup": "unknown"}]
    asyncio.run(exercise())


@posix_only
def test_posix_terminal_receipt_can_be_verified_after_restart(tmp_path):
    runtime, _, _ = _runtime(tmp_path)
    evidence = ShellRecoveryEvidence(tmp_path / "receipts", boot_provider=lambda: None)
    execution = evidence.prepare("run", "attempt", "call")
    request = ShellExecutionRequest(bind_call_execution(runtime.context, "call"), sys.executable,
        ("-c", "print('ok')"), recovery_evidence=execution, recovery_status_path=evidence.path(execution))
    result = PosixShellBackend().run(request)
    assert result.process_cleanup == "confirmed"
    proof = evidence.verify(execution, current_boot=None)
    assert proof["receipt"]["processGroupDrained"] is True
    forged = {**execution, "attempt_id": "different"}
    with pytest.raises(ValueError, match="尚无匹配"):
        evidence.verify(forged, current_boot=None)


@posix_only
def test_failed_process_launch_does_not_leave_unknown_receipt(tmp_path, monkeypatch):
    runtime, receipt, capture = _runtime(tmp_path)
    monkeypatch.setattr(workspace_tools.shutil, "which", lambda _: str(tmp_path / "missing"))
    result = _run("print('never')", runtime)
    assert result["status"] == "SANDBOX_UNAVAILABLE"
    assert result["process_cleanup"] == receipt["process_cleanup"] == "not_started"
    assert capture.changes == []


@posix_only
def test_builtin_sh_handles_pipeline_and_captures_files(tmp_path):
    runtime, receipt, capture = _runtime(tmp_path)
    result = json.loads(workspace_tools.sh.func(
        command="printf written > before.txt; printf pipeline | cat", runtime=runtime))
    assert result["exit_code"] == 0 and result["stdout"] == "pipeline"
    assert receipt["process_cleanup"] == "confirmed"
    assert capture.changes[0]["after"]["text"] == "written"


@posix_only
def test_cleanup_control_failure_keeps_barrier_and_suppresses_file_publication(tmp_path, monkeypatch):
    class LostConfirmation(PosixShellBackend):
        def _terminate_group(self, process):
            super()._terminate_group(process)
            raise OSError("lost exit confirmation")
    monkeypatch.setattr(workspace_tools, "_POSIX_SHELL_BACKEND", LostConfirmation())
    runtime, receipt, capture = _runtime(tmp_path)
    result = _run("from pathlib import Path; Path('before.txt').write_text('written')", runtime)
    assert result["status"] == "control_failed"
    assert result["process_cleanup"] == receipt["process_cleanup"] == "unknown"
    assert result["cleanup_warnings"]
    assert (tmp_path / "before.txt").read_text() == "written"
    assert capture.changes == []
