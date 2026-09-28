"""本文件对外提供 shell_policy_guidance，向模型说明当前文件模式与单次放宽规则。

输入为本次 Run 的真实工作区、当前模式和已装配能力；输出为可加入系统上下文的中文说明。
具体工作流为展示执行事实，指导模型在拒绝后选择足够的最小模式并说明理由，
同时提醒重试可能重复已完成的合法副作用；文本本身不产生授权。
示例：text = shell_policy_guidance(Path("C:/ws"), AccessMode.READ_ONLY, ("host_command",))。
"""

from pathlib import Path

from focus.security.policy import AccessMode


def shell_policy_guidance(workspace: Path, mode: AccessMode, permissions: tuple[str, ...]) -> str:
    ability = "、".join(permissions) if permissions else "无"
    return (
        "<focus_file_sandbox>\n"
        f"当前文件模式：{mode.value}；本次实际工作区：{workspace}；可用工具能力：{ability}。\n"
        "Shell 在 Windows 本机使用文件修改沙箱；其约束为部分约束，不保证读取保密、网络隔离或进程隐藏。"
        "工具能力与进程文件边界分别生效。\n"
        "遇到 FILE_POLICY_DENIED 时，不要自行重试为完全访问。"
        "只有当前调用确需扩大边界时，才在工具参数中提供 requested_mode 和非空 reason；"
        "选择足够完成任务的最小放宽：read-only 可申请 workspace-write 或 danger-full-access，"
        "workspace-write 仅可申请 danger-full-access。用户批准只对本次原样操作有效。"
        "重试可能重复拒绝前已经完成的合法副作用，应先核对执行结果。\n"
        "</focus_file_sandbox>"
    )
