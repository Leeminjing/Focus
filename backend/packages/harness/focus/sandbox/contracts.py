"""本文件对外提供本机 Shell 沙箱请求、结果和基础设施失败类型。

输入为不可变调用绑定、可执行文件、参数及超时；输出为逐调用执行事实或 SandboxUnavailable。
具体工作流为调用方构造请求，后端返回目标退出与输出，同时保留实际模式、后端状态和独立清理告警。
示例：ShellExecutionRequest(binding, "cmd.exe", ("/c", "echo ok"))。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from focus.security.execution import CallExecutionBinding
from focus.security.policy import AccessMode


class SandboxUnavailable(RuntimeError):
    pass


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True)
class ShellExecutionRequest:
    binding: CallExecutionBinding
    executable: str
    args: tuple[str, ...]
    timeout_seconds: float = 120
    cancel_event: CancellationSignal | None = None


@dataclass(frozen=True)
class ShellExecutionResult:
    mode: AccessMode
    workspace: Path
    backend_applied: bool
    enforcement: str
    stdout: str
    stderr: str
    exit_code: int | None
    status: str
    truncated: bool = False
    approval_id: str | None = None
    cleanup_warnings: tuple[str, ...] = ()
