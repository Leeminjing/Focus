"""本文件对外提供 checked_write_target，核对结构化文件工具的真实写入目标。

输入为服务端访问策略、用户路径和实际工作区；输出为可用同一绝对路径完成写入的目标。
具体工作流为解析已有符号链接与目录联接，检查当前文件模式，在建目录前拒绝非法目标；
建目录后调用方再次核对，降低路径布局变化造成的误判。
示例：target = checked_write_target(policy, "sub/result.txt")；target.write_text("ok")。
"""

from __future__ import annotations

from pathlib import Path

from focus.security.paths import canonical_target
from focus.security.policy import AccessDecision, AccessOperation, AccessPolicy, decide_path_access


def checked_write_target(policy: AccessPolicy, value: str) -> Path:
    target = canonical_target(policy.workspace, value)
    decision = decide_path_access(policy, target, AccessOperation.WRITE)
    if decision is AccessDecision.DENY:
        raise PermissionError(f"FILE_POLICY_DENIED: 当前模式 {policy.mode} 不允许修改 {target}")
    return target
