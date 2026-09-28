"""本文件对外提供 WindowsAclBackend，以固定 DSH 原语执行本机受限 Shell。

输入为服务端绑定的模式、真实工作区、目标参数和固定 DSH Node 依赖。
输出为目标输出与退出事实；后端缺失、授权失败或独立状态通道异常时抛 SandboxUnavailable。
具体工作流为验证后端、为可写会话准备独立临时授权，创建普通临时根下的状态文件，
再用无 Shell 插值的 argv 启动适配器；适配器借固定 DSH 创建受限令牌和受管理子进程，
状态文件区分目标退出与准备故障，超时关闭其 Job 以终止进程树。
示例：WindowsAclBackend().run(ShellExecutionRequest(binding, "cmd.exe", ("/c", "echo ok")))。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from focus.sandbox.contracts import SandboxUnavailable, ShellExecutionRequest, ShellExecutionResult
from focus.sandbox.prepared import PreparedWorkspaceRegistry
from focus.sandbox.session_temp import SessionTempRegistry
from focus.security.policy import AccessMode


class WindowsAclBackend:
    def __init__(
        self,
        *,
        node_binary: str | Path | None = None,
        runner_path: str | Path | None = None,
        grant_helper_path: str | Path | None = None,
        temp_root: str | Path | None = None,
        prepared_registry_path: str | Path | None = None,
    ) -> None:
        self._node_binary = str(node_binary) if node_binary is not None else os.environ.get("FOCUS_SANDBOX_NODE")
        self._runner_path = Path(runner_path) if runner_path is not None else self._default_runner()
        self._grant_helper_path = (
            Path(grant_helper_path) if grant_helper_path is not None else self._default_grant_helper()
        )
        self._temp_root = Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())
        self._registry = SessionTempRegistry()
        self._prepared = PreparedWorkspaceRegistry(
            Path(prepared_registry_path) if prepared_registry_path is not None else None
        )
        self._active: set[subprocess.Popen[bytes]] = set()
        self._lock = threading.RLock()
        self._closed = False

    def run(self, request: ShellExecutionRequest) -> ShellExecutionResult:
        with self._lock:
            if self._closed:
                raise SandboxUnavailable("Windows ACL 后端已关闭")
        node, runner, temp_root = self._require_backend(request)
        workspace = request.binding.workspace
        temp_dir = temp_root
        sid_args: list[str] = []
        if request.binding.mode is AccessMode.WORKSPACE_WRITE:
            helper = self._grant_helper_path.resolve()
            if not helper.is_file():
                raise SandboxUnavailable(f"未找到 Windows ACL 授权适配器: {helper}")
            grant = self._registry.prepare(request.binding, node, helper, temp_root)
            self._prepared.record(grant.workspace)
            workspace = grant.workspace
            temp_dir = grant.temp_dir
            sid_args = [grant.write_sid, grant.temp_write_sid]
        else:
            sid_args = ["", ""]
        try:
            status_fd, status_name = tempfile.mkstemp(
                prefix="focus-sandbox-status-", suffix=".json", dir=temp_root,
            )
        except OSError as error:
            raise SandboxUnavailable(f"Windows ACL 状态通道创建失败: {error}") from error
        os.close(status_fd)
        status_path = Path(status_name)
        argv = [
            node, str(runner), str(status_path), str(workspace),
            str(temp_dir), str(request.binding.mode),
            *sid_args, "--", request.executable, *request.args,
        ]
        try:
            try:
                with self._lock:
                    if self._closed:
                        raise SandboxUnavailable("Windows ACL 后端已关闭")
                    process = subprocess.Popen(
                        argv, cwd=request.binding.workspace, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
                        env=os.environ.copy(),
                    )
                    self._active.add(process)
            except OSError as error:
                raise SandboxUnavailable(f"Windows ACL runner 启动失败: {error}") from error
            try:
                status, stdout, stderr = self._wait(process, request)
                if status != "exited":
                    return self._result(request, stdout, stderr, None, status)
                fact = self._read_status(status_path)
                if fact.get("phase") == "failed":
                    raise SandboxUnavailable(str(fact.get("error") or "Windows ACL 准备失败"))
                if fact.get("phase") != "exited" or fact.get("exitCode") != process.returncode:
                    raise SandboxUnavailable(f"Windows ACL runner 未完成目标退出协议: {fact}")
                return self._result(request, stdout, stderr, process.returncode, status)
            finally:
                with self._lock:
                    self._active.discard(process)
        finally:
            status_path.unlink(missing_ok=True)

    def close(self) -> tuple[str, ...]:
        with self._lock:
            self._closed = True
            active = tuple(self._active)
        for process in active:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        node = self._node_binary or shutil.which("node")
        if not node:
            return ("无法撤销私有临时授权：Node 运行时不可用",)
        return self._registry.close(node, self._grant_helper_path.resolve(), self._temp_root.resolve())

    def prepared_workspaces(self) -> tuple[str, ...]:
        return self._prepared.list()

    @staticmethod
    def _read_status(path: Path) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SandboxUnavailable(f"Windows ACL 状态通道不可用: {error}") from error
        if not isinstance(value, dict):
            raise SandboxUnavailable("Windows ACL 状态协议不是对象")
        return value

    @staticmethod
    def _wait(process: subprocess.Popen[bytes], request: ShellExecutionRequest) -> tuple[str, bytes, bytes]:
        deadline = time.monotonic() + request.timeout_seconds
        while True:
            if request.cancel_event is not None and request.cancel_event.is_set():
                status = "cancelled"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "timeout"
                break
            try:
                stdout, stderr = process.communicate(timeout=min(remaining, 0.2))
                return "exited", stdout, stderr
            except subprocess.TimeoutExpired:
                continue
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate()
        return status, stdout, stderr

    def _require_backend(self, request: ShellExecutionRequest) -> tuple[str, Path, Path]:
        if os.name != "nt":
            raise SandboxUnavailable("Windows ACL 后端只支持 Windows 原生执行")
        if request.binding.mode not in (AccessMode.READ_ONLY, AccessMode.WORKSPACE_WRITE):
            raise ValueError("Windows ACL 后端仅处理受限模式")
        node = self._node_binary or shutil.which("node")
        if not node or not Path(node).is_file():
            raise SandboxUnavailable("未找到 Node 运行时")
        runner = self._runner_path.resolve()
        if not runner.is_file():
            raise SandboxUnavailable(f"未找到 Windows ACL runner: {runner}")
        dsh_package = self._default_dsh_package()
        if not dsh_package.is_file():
            raise SandboxUnavailable(f"未找到固定 DSH Windows ACL 依赖: {dsh_package}")
        try:
            temp_root = self._temp_root.resolve(strict=True)
        except OSError as error:
            raise SandboxUnavailable(f"临时根不可用: {self._temp_root}") from error
        if not temp_root.is_dir():
            raise SandboxUnavailable(f"临时根不是目录: {temp_root}")
        return node, runner, temp_root

    @staticmethod
    def _default_runner() -> Path:
        return Path(__file__).resolve().parents[5] / "desktop" / "windows-sandbox-run.mjs"

    @staticmethod
    def _default_dsh_package() -> Path:
        return (
            Path(__file__).resolve().parents[5] / "desktop" / "node_modules"
            / "@deepseek-ai" / "dsh-sandbox-windows-acl" / "lib" / "index.js"
        )

    @staticmethod
    def _default_grant_helper() -> Path:
        return Path(__file__).resolve().parents[5] / "desktop" / "windows-sandbox-grant.mjs"

    @staticmethod
    def _result(
        request: ShellExecutionRequest, stdout: bytes, stderr: bytes,
        exit_code: int | None, status: str,
    ) -> ShellExecutionResult:
        return ShellExecutionResult(
            mode=request.binding.mode, workspace=request.binding.workspace,
            backend_applied=True, enforcement="partial",
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            exit_code=exit_code, status=status,
            approval_id=request.binding.approval_id,
        )
