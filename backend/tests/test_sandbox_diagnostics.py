"""本文件对外提供 Shell 结果分类和观察边界测试。

输入为真实退出状态、受限标志和进程标准错误；输出为可区分的失败类别。
具体工作流为验证启动前故障、启动后控制故障、取消、超时、程序错误与文件拒绝诊断，
并确认捕获拒绝后退出 0 不被误报为整条命令失败。
示例：运行 python -m pytest backend/tests/test_sandbox_diagnostics.py。
"""

from focus.sandbox.diagnostics import classify_shell_outcome


def test_infrastructure_and_process_failures_are_distinct():
    assert classify_shell_outcome("SANDBOX_UNAVAILABLE", None, False, "missing") == (
        "sandbox_unavailable", False,
    )
    assert classify_shell_outcome("control_failed", None, True, "lost status") == (
        "sandbox_control_failed_after_start", False,
    )
    assert classify_shell_outcome("cancelled", None, True, "") == ("cancelled", False)
    assert classify_shell_outcome("timeout", None, True, "") == ("timeout", False)
    assert classify_shell_outcome("exited", 2, True, "syntax error") == ("program_error", False)


def test_access_denial_is_observed_without_claiming_full_audit():
    stderr = "PermissionError: [Errno 13] Permission denied"
    assert classify_shell_outcome("exited", 1, True, stderr) == (
        "file_policy_denied_observed", True,
    )
    assert classify_shell_outcome("exited", 0, True, stderr) == (None, True)
    assert classify_shell_outcome("exited", 0, True, "") == (None, False)
    assert classify_shell_outcome("exited", 1, False, stderr) == ("program_error", False)
