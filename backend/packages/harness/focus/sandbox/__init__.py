"""本文件对外提供 Focus 本机 Shell 执行结果、失败类型与平台后端入口。

输入为受治理的逐调用绑定和目标程序参数；输出为执行结果或明确的准备失败。
具体工作流为由调用方选取平台后端，再由后端验证边界、启动并管理目标进程。
示例：WindowsAclBackend().run(ShellExecutionRequest(binding, command, args))。
"""

from focus.sandbox.contracts import SandboxUnavailable, ShellExecutionRequest, ShellExecutionResult
from focus.sandbox.posix import PosixShellBackend
from focus.sandbox.windows import WindowsAclBackend

__all__ = ["SandboxUnavailable", "ShellExecutionRequest", "ShellExecutionResult", "WindowsAclBackend", "PosixShellBackend"]
