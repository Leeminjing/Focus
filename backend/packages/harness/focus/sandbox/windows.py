"""本文件对外提供 WindowsAclBackend，以 Focus 自有 Windows 后端执行本机受限 Shell。

输入为服务端绑定的模式、真实工作区、目标参数和 Focus 自有 Node 桥。
输出为目标输出、退出或启动后控制故障事实；启动前后端缺失或授权失败时抛 SandboxUnavailable。
具体工作流为验证后端、先登记并排除重叠工作区、再准备独立临时授权，创建状态文件，
再用无 Shell 插值的 argv 启动适配器；适配器构造 Low 限制令牌和受管理子进程，
状态文件区分目标退出与准备故障，超时关闭其 Job 以终止进程树。
示例：WindowsAclBackend().run(ShellExecutionRequest(binding, "cmd.exe", ("/c", "echo ok")))。
"""

from __future__ import annotations

import json
import logging
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


_log = logging.getLogger(__name__)


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
            self._prepared.record(workspace)
            grant = self._registry.prepare(request.binding, node, helper, temp_root)
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
                    fact = self._read_status(status_path)
                    if fact.get("phase") not in ("started", "exited"):
                        raise SandboxUnavailable(f"Windows ACL 目标未确认受限启动: {fact}")
                    return self._result(request, stdout, stderr, None, status)
                fact = self._read_status(status_path)
                if fact.get("phase") == "failed":
                    raise SandboxUnavailable(str(fact.get("error") or "Windows ACL 准备失败"))
                if fact.get("phase") == "control_failed" or (
                    fact.get("phase") == "started" and process.returncode == 126
                ):
                    code = fact.get("exitCode")
                    warning = str(fact.get("error") or "Windows ACL 控制端在目标启动后失败")
                    return self._result(
                        request, stdout, stderr, code if type(code) is int else None,
                        "control_failed", (warning,),
                    )
                if fact.get("phase") != "exited":
                    raise SandboxUnavailable(f"Windows ACL runner 未完成目标退出协议: {fact}")
                if type(fact.get("exitCode")) is not int:
                    raise SandboxUnavailable(f"Windows ACL 目标退出码协议无效: {fact}")
                if fact.get("exitCode") != process.returncode and process.returncode != 126:
                    raise SandboxUnavailable(f"Windows ACL runner 退出码与目标结果不一致: {fact}")
                warnings = fact.get("cleanupWarnings", [])
                if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
                    raise SandboxUnavailable("Windows ACL 清理告警协议无效")
                if process.returncode == 126 and fact["exitCode"] != 126:
                    warnings.append("Windows ACL 控制端在目标退出后发生故障")
                for warning in warnings:
                    _log.warning("Windows sandbox cleanup: %s", warning)
                return self._result(request, stdout, stderr, fact["exitCode"], status, tuple(warnings))
            finally:
                with self._lock:
                    self._active.discard(process)
        finally:
            for cleanup_path in (status_path, status_path.with_name(status_path.name + ".pending")):
                try:
                    cleanup_path.unlink(missing_ok=True)
                except OSError as error:
                    _log.warning("Windows sandbox status cleanup: %s", error)

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
            warnings = ("无法撤销私有临时授权：Node 运行时不可用",)
        else:
            warnings = self._registry.close(node, self._grant_helper_path.resolve(), self._temp_root.resolve())
        for warning in warnings:
            _log.warning("Windows sandbox cleanup: %s", warning)
        return warnings

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
        native_module = self._default_native_module()
        if not native_module.is_file():
            raise SandboxUnavailable(f"未找到 Focus Windows 原生模块: {native_module}")
        koffi_package = self._default_koffi_package()
        if not koffi_package.is_file():
            raise SandboxUnavailable(f"未找到 Focus Windows Koffi 依赖: {koffi_package}")
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
    def _default_native_module() -> Path:
        return Path(__file__).resolve().parents[5] / "desktop" / "windows-sandbox" / "win32.mjs"

    @staticmethod
    def _default_koffi_package() -> Path:
        return Path(__file__).resolve().parents[5] / "desktop" / "node_modules" / "koffi" / "package.json"

    @staticmethod
    def _default_grant_helper() -> Path:
        return Path(__file__).resolve().parents[5] / "desktop" / "windows-sandbox-grant.mjs"

    @staticmethod
    def _result(
        request: ShellExecutionRequest, stdout: bytes, stderr: bytes,
        exit_code: int | None, status: str, cleanup_warnings: tuple[str, ...] = (),
    ) -> ShellExecutionResult:
        return ShellExecutionResult(
            mode=request.binding.mode, workspace=request.binding.workspace,
            backend_applied=True, enforcement="partial",
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            exit_code=exit_code, status=status,
            approval_id=request.binding.approval_id,
            cleanup_warnings=cleanup_warnings,
        )
