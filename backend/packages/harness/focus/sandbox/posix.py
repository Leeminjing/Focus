"""POSIX 完全访问 Shell：取消、超时和进程组清理共享执行结果合同。

目标以独立 session 启动；结束时先 TERM 再 KILL 整个进程组，只有组已消失且
输出管道关闭才确认清理。完全访问不阻止程序主动脱离 session，不提供文件沙箱。
受限模式明确失败，由既有单次审批决定是否允许完全访问。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time

from focus.sandbox.contracts import SandboxUnavailable, ShellExecutionRequest, ShellExecutionResult
from focus.security.policy import AccessMode


class PosixShellBackend:
    def run(self, request: ShellExecutionRequest) -> ShellExecutionResult:
        if request.binding.mode is not AccessMode.DANGER_FULL_ACCESS:
            raise SandboxUnavailable(
                "当前平台尚不支持 read-only / workspace-write 受限 Shell，命令未执行。"
                "若确需执行，请说明无文件限制的影响，以 requested_mode='danger-full-access' "
                "和非空 reason 申请用户单次批准；也可使用已有文件工具。禁止自动切换或重试。"
            )
        if self._cancelled(request):
            return self._result(request, "cancelled", None, "", "", "not_started")
        if os.name != "posix":
            raise SandboxUnavailable("POSIX Shell 后端仅支持 POSIX 宿主机")
        try:
            process = subprocess.Popen(
                [request.executable, *request.args], cwd=request.binding.workspace,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors="replace", close_fds=True, start_new_session=True,
            )
        except OSError as error:
            raise SandboxUnavailable(f"POSIX Shell 启动失败，命令未执行: {error}") from error

        status, stdout, stderr = "exited", "", ""
        warnings = []
        cleanup = "unknown"
        deadline = time.monotonic() + request.timeout_seconds
        try:
            while True:
                if self._cancelled(request):
                    status = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    status = "timeout"
                    break
                try:
                    stdout, stderr = process.communicate(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            # communicate 返回只证明父进程和管道结束；重定向输出的后台子进程仍需清理。
            try:
                self._terminate_group(process)
                stdout, stderr = process.communicate(timeout=3)
                cleanup = "confirmed"
            except (OSError, subprocess.TimeoutExpired) as error:
                warnings.append(f"POSIX 进程组清理未确认: {error}")
                if isinstance(error, subprocess.TimeoutExpired):
                    stdout = self._text(error.stdout)
                    stderr = self._text(error.stderr)
                # 无法证明退出时保持 unknown，不因关闭本地管道宣称目标已停止。
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        if cleanup != "confirmed":
            status = "control_failed"
        return self._result(request, status, process.returncode, stdout, stderr, cleanup, tuple(warnings))

    @staticmethod
    def _cancelled(request: ShellExecutionRequest) -> bool:
        if request.cancel_event is not None and request.cancel_event.is_set():
            return True
        path = request.recovery_status_path
        return path is not None and path.with_name(path.name + ".cancel").exists()

    @staticmethod
    def _terminate_group(process: subprocess.Popen) -> None:
        def signal_group(value):
            try:
                os.killpg(process.pid, value)
                return True
            except ProcessLookupError:
                return False

        if not signal_group(signal.SIGTERM):
            process.wait(timeout=3)
            return
        for value, grace in ((None, 0.3), (signal.SIGKILL, 3)):
            if value is not None and not signal_group(value):
                process.wait(timeout=3)
                return
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                process.poll()  # 回收直接子进程，避免把自己的 zombie 当成仍存活的组。
                if not signal_group(0):
                    process.wait(timeout=3)
                    return
                time.sleep(0.01)
        raise TimeoutError("发送 SIGKILL 后进程组仍存在")

    @staticmethod
    def _text(value) -> str:
        return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

    @staticmethod
    def _result(request, status, code, stdout, stderr, cleanup, warnings=()):
        # 回执绑定服务端执行 identity；写入失败不抹去内存中已取得的退出证据。
        if request.recovery_status_path is not None and request.recovery_evidence is not None:
            path = request.recovery_status_path
            pending = path.with_name(path.name + ".pending")
            try:
                pending.write_text(json.dumps({"execution": request.recovery_evidence,
                    "phase": "failed" if cleanup == "not_started" else "exited",
                    "processGroupDrained": cleanup == "confirmed", "termination": status,
                    "exitCode": code}), encoding="utf-8")
                pending.replace(path)
            except OSError as error:
                warnings = (*warnings, f"POSIX 退出回执保存失败: {error}")
        return ShellExecutionResult(request.binding.mode, request.binding.workspace, False, "none",
            stdout, stderr, code, status, cleanup_warnings=warnings, process_cleanup=cleanup)
