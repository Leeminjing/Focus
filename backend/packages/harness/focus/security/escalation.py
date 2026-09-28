"""本文件对外提供 validate_escalation 和 escalation_request_id，定义单次模式放宽契约。

输入为当前模式、目标模式、理由、不可变调用绑定以及原始工具参数。
输出为合法的更宽目标模式及绑定本次主体、工作区、命令或路径的请求标识。
具体工作流为先按三档顺序拒绝同级或降级，再对规范化载荷计算稳定摘要；
摘要只用于核对审批对象，不充当跨调用可复用的授权凭证。
示例：mode = validate_escalation(AccessMode.READ_ONLY, "workspace-write", "生成结果")。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from focus.security.execution import CallExecutionBinding
from focus.security.policy import AccessMode


_RANK = {
    AccessMode.READ_ONLY: 0,
    AccessMode.WORKSPACE_WRITE: 1,
    AccessMode.DANGER_FULL_ACCESS: 2,
}


def validate_escalation(current: AccessMode, target: str, reason: str) -> AccessMode:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("申请更宽模式必须提供非空理由")
    try:
        desired = AccessMode(target)
    except ValueError as error:
        raise ValueError(f"无效的目标模式: {target}") from error
    if target != desired.value:
        raise ValueError(f"目标模式必须使用当前名称: {desired.value}")
    if _RANK[desired] <= _RANK[current]:
        raise ValueError(f"目标模式 {desired} 必须宽于当前模式 {current}")
    return desired


def escalation_request_id(
    binding: CallExecutionBinding, tool: str, args: Mapping[str, Any], target: AccessMode,
) -> str:
    content = {
        "run_id": binding.run_id,
        "session_id": binding.session_id,
        "call_id": binding.call_id,
        "agent_id": binding.agent_id,
        "workspace": str(binding.workspace),
        "current_mode": str(binding.mode),
        "target_mode": str(target),
        "tool": tool,
        "args": dict(args),
    }
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
