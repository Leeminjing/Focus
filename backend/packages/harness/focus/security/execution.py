"""本文件对外提供 CallExecutionBinding 和 bind_call_execution，作为本机调用的不可变执行事实。

输入为服务端签发的 SecurityContext、工具调用标识及受治理的 Loop 租约。
输出为绑定 Run、执行会话、主体、真实工作区、模式与租约的 CallExecutionBinding。
具体工作流为验证身份和现存工作区，解析真实路径，读取受治理模式并冻结各字段；
后续并发调用或会话模式变化不能改写已有绑定。
示例：binding = bind_call_execution(runtime.context, "call-1")；binding.mode 为实际启动模式。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from focus.security.context import security_context_of
from focus.security.policy import AccessMode


@dataclass(frozen=True)
class CallExecutionBinding:
    run_id: str
    session_id: str
    call_id: str
    agent_id: str
    agent_role: str
    workspace: Path
    mode: AccessMode
    mode_source: str
    lease_id: str | None = None
    lease_fencing_token: int | None = None
    approval_id: str | None = None


def bind_call_execution(context: object, call_id: str) -> CallExecutionBinding:
    security = security_context_of(context)
    routing = security.routing
    if not all((routing.run_id, routing.thread_id, routing.agent_id, call_id)):
        raise RuntimeError("缺少 Run、执行会话、执行主体或调用身份")
    workspace = Path(security.authorization.workspace).resolve(strict=True)
    if not workspace.is_dir():
        raise RuntimeError(f"实际工作区不是目录: {workspace}")
    values = context if isinstance(context, Mapping) else {}
    lease = values.get("workspace_lease")
    lease_id = str(lease["lease_id"]) if isinstance(lease, Mapping) else None
    fencing = int(lease["fencing_token"]) if isinstance(lease, Mapping) else None
    return CallExecutionBinding(
        run_id=routing.run_id,
        session_id=routing.thread_id,
        call_id=call_id,
        agent_id=routing.agent_id,
        agent_role=security.authorization.agent_role,
        workspace=workspace,
        mode=security.authorization.access_mode,
        mode_source=str(security.extras.get("approved_mode_source") or security.extras.get("mode_source") or "execution-profile"),
        lease_id=lease_id,
        lease_fencing_token=fencing,
        approval_id=str(security.extras["approval_id"]) if security.extras.get("approval_id") else None,
    )
