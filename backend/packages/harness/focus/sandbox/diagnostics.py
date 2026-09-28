"""本文件对外提供 classify_shell_outcome，区分执行状态与可观察的文件拒绝诊断。

输入为后端状态、退出码、是否受限和标准错误；输出为失败类别及是否观察到拒绝诊断。
具体工作流为优先保留取消、超时和后端故障事实，再识别常见 Windows/Python/Node
访问拒绝诊断；仅把文本视为观察证据，不把缺失诊断解释为没有发生拒绝。
示例：classify_shell_outcome("exited", 1, True, "PermissionError: [Errno 13]")。
"""

import re


_DENIAL = re.compile(
    r"(?i)(?:PermissionError:\s*\[Errno\s*13\]|\[WinError\s*5\]|"
    r"\bEACCES\b|\bEPERM\b|UnauthorizedAccessException|"
    r"Access(?:\s+to\s+[^\r\n]+)?\s+is\s+denied|Permission\s+denied)"
)


def classify_shell_outcome(
    status: str, exit_code: int | None, applied: bool, stderr: str,
) -> tuple[str | None, bool]:
    observed = applied and bool(_DENIAL.search(stderr))
    if status == "SANDBOX_UNAVAILABLE":
        return "sandbox_unavailable", observed
    if status in ("cancelled", "timeout"):
        return status, observed
    if exit_code is None or exit_code == 0:
        return None, observed
    return ("file_policy_denied_observed" if observed else "program_error"), observed
