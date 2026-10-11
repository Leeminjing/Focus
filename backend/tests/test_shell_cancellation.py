"""本文件对外提供 Shell 取消、超时与执行清理屏障的定向验收。

输入为真实 Windows 三档权限、含普通子进程的长运行目标与重复异步取消；输出为父子进程均已退出及中间件未提前返回的证据。
具体工作流使用独立 NTFS 夹具启动目标，触发取消或超时，返回后以 Win32 进程句柄核实退出；中间件测试用独立清理闸门证明取消传播等待收口。
示例：python -m pytest backend/tests/test_shell_cancellation.py -q；非 Windows 仅跳过真实 Job 用例。
"""

import asyncio
import ctypes
import os
import re
import sys
import threading
from dataclasses import replace

import pytest
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from backend.tests.runtime_context_support import runtime_context
from focus.sandbox import ShellExecutionRequest, WindowsAclBackend
from focus.security.context import security_context_of
from focus.security.execution import CallExecutionBinding
from focus.security.middleware import AccessPolicyMiddleware
from focus.security.policy import AccessMode
from focus.tools.builtins.workspace_tools import powershell
from windows_sandbox_fixture import windows_sandbox_roots


def _assert_exited(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 0) == 0, f"进程 {pid} 仍在执行"
        finally:
            kernel.CloseHandle(handle)
    else:
        assert ctypes.get_last_error() == 87


@pytest.mark.skipif(os.name != "nt", reason="真实 Windows Job 取消验收")
@pytest.mark.parametrize("mode", [AccessMode.READ_ONLY, AccessMode.WORKSPACE_WRITE, AccessMode.FULL])
@pytest.mark.parametrize("termination", ["cancelled", "timeout", "backend_closed"])
def test_three_modes_wait_for_parent_and_child_process_exit(windows_sandbox_roots, mode, termination):
    roots = windows_sandbox_roots
    backend = WindowsAclBackend(prepared_registry_path=roots.workspace_a.parent / "prepared.json")
    binding = CallExecutionBinding(run_id="cancel-run", session_id="cancel-session", call_id="cancel-call",
        agent_id="cancel-agent", agent_role="main", workspace=roots.workspace_a, mode=mode, mode_source="patrol")
    script = "import os,subprocess,sys,time; print('PARENT',os.getpid(),flush=True); subprocess.Popen([sys.executable,'-c',\"import os,time; print('CHILD',os.getpid(),flush=True); time.sleep(30)\"]); time.sleep(30)"
    cancellation = threading.Event()
    closed = []
    timer = threading.Timer(2, lambda: closed.append(backend.close()) if termination == "backend_closed" else cancellation.set())
    try:
        if termination != "timeout":
            timer.start()
        result = backend.run(ShellExecutionRequest(binding, sys.executable, ("-B", "-c", script),
            timeout_seconds=2 if termination == "timeout" else 10, cancel_event=cancellation))
        assert result.status == ("cancelled" if termination == "backend_closed" else termination), result
        pids = [int(item) for item in re.findall(r"(?:PARENT|CHILD) (\d+)", result.stdout)]
        assert len(pids) == 2, (result.stdout, result.stderr)
        for pid in pids:
            _assert_exited(pid)
        assert result.backend_applied is (mode is not AccessMode.FULL)
    finally:
        timer.cancel()
        if timer.ident is not None:
            timer.join()
        assert backend.close() == ()
    if termination == "backend_closed":
        assert closed == [()]


def test_repeated_async_cancel_waits_for_shell_cleanup_and_keeps_parent_signal(tmp_path):
    async def run():
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        released = threading.Event()
        parent = threading.Event()
        context = runtime_context(agent_id="main", task_id="task", workspace=str(tmp_path), permissions=("host_command",))
        security = security_context_of(context)
        context["security_context"] = replace(security, extras={**security.extras, "sandbox_cancel_event": parent})
        runtime = ToolRuntime(state={}, context=context, config={}, stream_writer=None, tool_call_id="call", store=None, tools=[])
        request = ToolCallRequest(tool_call={"name": "powershell", "args": {"command": "echo controlled"}, "id": "call"},
            tool=powershell, state={}, runtime=runtime)
        async def handler(bound):
            signal = security_context_of(bound.runtime.context).extras["sandbox_cancel_event"]
            entered.set()
            while not signal.is_set():
                await asyncio.sleep(0.001)
            cleaning.set()
            await asyncio.to_thread(released.wait)
            return ToolMessage(content="process tree drained", tool_call_id="call")
        task = asyncio.create_task(AccessPolicyMiddleware().awrap_tool_call(request, handler))
        await entered.wait()
        parent.set()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
