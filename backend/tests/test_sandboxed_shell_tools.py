"""本文件对外提供内置 Shell 的逐调用模式和后端接入测试。

输入为受治理运行上下文、真实 Windows 工作区与可替换受限后端。
输出为结构化执行事实，验证受限写入、只读拒写和后端故障时不执行目标命令。
具体工作流为以 ToolRuntime 注入调用标识，调用内置 PowerShell，再核对真实文件内容与返回状态。
示例：运行 python -m pytest backend/tests/test_sandboxed_shell_tools.py。
"""

import json
import os
from pathlib import Path

import pytest
from langchain.tools import ToolRuntime

from backend.tests.runtime_context_support import runtime_context
from focus.sandbox import WindowsAclBackend
from focus.security.policy import AccessMode
from focus.tools.builtins import workspace_tools
from windows_sandbox_fixture import windows_sandbox_roots


def _runtime(workspace: Path, mode: AccessMode) -> ToolRuntime:
    return ToolRuntime(
        state={},
        context=runtime_context(
            agent_id="agent-1", task_id="task-1", workspace=str(workspace),
            permissions=("read", "host_command"), access_mode=mode,
        ),
        config={}, stream_writer=None, tool_call_id="call-1", store=None, tools=[],
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows 内置 Shell 验收")
def test_powershell_uses_bound_workspace_and_mode(windows_sandbox_roots, monkeypatch):
    roots = windows_sandbox_roots
    backend = WindowsAclBackend(prepared_registry_path=roots.workspace_a.parent / "prepared.json")
    monkeypatch.setattr(workspace_tools, "_SHELL_BACKEND", backend)
    target = roots.workspace_a / "shell.txt"
    command = f"[IO.File]::WriteAllText('{target}', 'written')"
    try:
        writable = json.loads(workspace_tools.powershell.func(
            command=command, runtime=_runtime(roots.workspace_a, AccessMode.WORKSPACE_WRITE),
        ))
        assert writable["backend_applied"] is True
        assert writable["enforcement"] == "partial"
        assert writable["mode"] == "workspace-write"
        assert writable["run_id"] == "run-1"
        assert writable["session_id"] == "thread-1"
        assert writable["call_id"] == "call-1"
        assert writable["agent_id"] == "agent-1"
        assert writable["workspace"] == str(roots.workspace_a)
        assert writable["mode_source"] == "execution-profile"
        assert writable["status"] == "exited"
        assert writable["approval_id"] is None
        assert writable["truncated"] is False
        assert writable["cleanup_warnings"] == []
        assert writable["exit_code"] == 0
        assert target.read_text() == "written"
        readonly = json.loads(workspace_tools.powershell.func(
            command=f"[IO.File]::WriteAllText('{target}', 'escaped')",
            runtime=_runtime(roots.workspace_a, AccessMode.READ_ONLY),
        ))
        assert readonly["backend_applied"] is True
        assert readonly["mode"] == "read-only"
        assert readonly["exit_code"] != 0
        assert target.read_text() == "written"
    finally:
        assert backend.close() == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows 内置 Shell 验收")
def test_missing_backend_does_not_run_builtin_shell(windows_sandbox_roots, monkeypatch):
    roots = windows_sandbox_roots
    marker = roots.workspace_a / "must-not-run.txt"
    monkeypatch.setattr(
        workspace_tools, "_SHELL_BACKEND",
        WindowsAclBackend(runner_path=roots.workspace_a / "missing-runner.js"),
    )
    fact = json.loads(workspace_tools.powershell.func(
        command=f"[IO.File]::WriteAllText('{marker}', 'ran')",
        runtime=_runtime(roots.workspace_a, AccessMode.WORKSPACE_WRITE),
    ))
    assert fact["status"] == "SANDBOX_UNAVAILABLE"
    assert fact["backend_applied"] is False
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows CMD 与完全访问验收")
def test_cmd_is_confined_and_full_access_is_explicit(windows_sandbox_roots, monkeypatch):
    roots = windows_sandbox_roots
    backend = WindowsAclBackend(prepared_registry_path=roots.workspace_a.parent / "prepared.json")
    monkeypatch.setattr(workspace_tools, "_SHELL_BACKEND", backend)
    try:
        confined = json.loads(workspace_tools.cmd.func(
            command="echo CMD_READY",
            runtime=_runtime(roots.workspace_a, AccessMode.WORKSPACE_WRITE),
        ))
        assert confined["backend_applied"] is True
        assert confined["exit_code"] == 0
        assert "CMD_READY" in confined["output"]
        full = json.loads(workspace_tools.powershell.func(
            command=f"[IO.File]::WriteAllText('{roots.outside_file}', 'full access')",
            runtime=_runtime(roots.workspace_a, AccessMode.DANGER_FULL_ACCESS),
        ))
        assert full["backend_applied"] is False
        assert full["enforcement"] == "none"
        assert full["mode"] == "danger-full-access"
        assert full["status"] == "exited"
        assert full["exit_code"] == 0
        assert roots.outside_file.read_text() == "full access"
    finally:
        assert backend.close() == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows Shell 结果事实验收")
def test_result_preserves_program_error_caught_denial_and_truncation(windows_sandbox_roots, monkeypatch):
    roots = windows_sandbox_roots
    backend = WindowsAclBackend(prepared_registry_path=roots.workspace_a.parent / "prepared.json")
    monkeypatch.setattr(workspace_tools, "_SHELL_BACKEND", backend)
    runtime = _runtime(roots.workspace_a, AccessMode.WORKSPACE_WRITE)
    try:
        program_error = json.loads(workspace_tools.powershell.func(command="throw 'program failed'", runtime=runtime))
        assert program_error["exit_code"] != 0
        assert program_error["failure_kind"] == "program_error"
        caught = json.loads(workspace_tools.powershell.func(
            command=f"try {{ [IO.File]::WriteAllText('{roots.outside_file}', 'escaped') }} catch {{ Write-Output 'CAUGHT_DENIAL' }}",
            runtime=runtime,
        ))
        assert caught["exit_code"] == 0
        assert "CAUGHT_DENIAL" in caught["stdout"]
        assert roots.outside_file.read_text() == "outside unchanged"
        large = json.loads(workspace_tools.powershell.func(
            command="Write-Output ('x' * 40000)", runtime=runtime,
        ))
        assert large["exit_code"] == 0
        assert large["truncated"] is True
        assert len(large["output"]) == 30000
    finally:
        assert backend.close() == ()
