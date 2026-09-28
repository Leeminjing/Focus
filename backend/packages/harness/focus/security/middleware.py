"""本文件对外提供 AccessPolicyMiddleware，统一执行工具准入、文件拒绝和业务审批。

输入为工具调用、受治理执行上下文、效果契约与下游执行器。
输出为真实工具结果、文件策略拒绝或明确的审批结局。
具体工作流为先复查 Loop 租约与受治理身份、能力，再解析结构化目标并按模式拒绝非法写入，随后处理
敏感路径和未受限的不透明效果审批，最后把已检查的参数交给工具执行。
示例：AccessPolicyMiddleware().wrap_tool_call(request, handler) 返回 ToolMessage 或真实结果。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command, interrupt

from focus.security.approval import ApprovalRequest, approval_granted
from focus.security.context import security_context_of
from focus.security.escalation import escalation_request_id, validate_escalation
from focus.security.execution import bind_call_execution
from focus.security.live_mode import refreshed_runtime_context
from focus.security.effects import EffectContract, ToolEffectKind, effect_of, resolve_fs_effect
from focus.security.policy import (
    AccessDecision,
    AccessMode,
    AccessOperation,
    AccessPolicy,
    decide_path_access,
    policy_from_context,
)

ToolResult = ToolMessage | Command[Any]


@dataclass(frozen=True)
class _Admission:
    """一次调用的准入结论：是否需要人工批准，以及应当下发的参数。

    读目标与写目标分开保留：人需要知道某个路径会被读还是会被改写，因此待决载荷
    不做无标注的合并。
    """

    asked: bool
    args: Mapping[str, Any]
    reads: tuple[Path, ...] = ()
    writes: tuple[Path, ...] = ()
    request: ApprovalRequest | None = None
    denied: tuple[Path, ...] = ()
    requested_mode: AccessMode | None = None
    capability_denied: str | None = None

    @property
    def targets(self) -> tuple[Path, ...]:
        return (*self.reads, *self.writes)


class AccessPolicyMiddleware(AgentMiddleware):
    """唯一准入点：在工具执行前判定，待决交回人类，获批只作用于当前这一次调用。"""

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolResult],
    ) -> ToolResult:
        _reject_sync_workspace_lease(request)
        admission = _admit(request)
        if admission.capability_denied:
            return _capability_denied(request, admission.capability_denied)
        if admission.denied:
            return _policy_denied(request, admission)
        if admission.asked:
            outcome = _approval_outcome(admission)
            if outcome != "approved":
                return _denied(request, admission, outcome)
        return handler(_with_grant(_with_args(request, admission.args), admission))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolResult]],
    ) -> ToolResult:
        request = request.override(
            runtime=replace(
                request.runtime,
                context=await refreshed_runtime_context(request.runtime.context),
            )
        )
        await _assert_workspace_lease(request)
        admission = _admit(request)
        if admission.capability_denied:
            return _capability_denied(request, admission.capability_denied)
        if admission.denied:
            return _policy_denied(request, admission)
        if admission.asked:
            outcome = _approval_outcome(admission)
            if outcome != "approved":
                return _denied(request, admission, outcome)
        if admission.requested_mode is not None:
            await _assert_workspace_lease(request)
        return await handler(_with_grant(_with_args(request, admission.args), admission))


async def _assert_workspace_lease(request: ToolCallRequest) -> None:
    context = _context_of(request)
    lease = context.get("workspace_lease")
    guard = context.get("workspace_lease_guard")
    if not isinstance(lease, Mapping) or not callable(guard):
        return
    await guard(str(lease["lease_id"]), int(lease["fencing_token"]))


def _reject_sync_workspace_lease(request: ToolCallRequest) -> None:
    if isinstance(_context_of(request).get("workspace_lease"), Mapping):
        raise RuntimeError("Loop workspace lease 必须通过异步工具边界验证")


def _approval_outcome(admission: _Admission) -> str:
    """读取本次审批结果并区分拒绝、取消、失配与渠道故障。"""
    if admission.request is None:
        return "unavailable"
    try:
        value = interrupt(admission.request.payload())
    except RuntimeError:
        return "unavailable"
    if not isinstance(value, Mapping):
        return "unavailable"
    decision = str(value.get("decision") or "")
    if decision == "cancel":
        return "cancelled"
    if decision != "approve":
        return "rejected" if decision == "reject" else "unavailable"
    if admission.requested_mode is not None:
        return "approved" if value.get("request_id") == admission.request.request_id else "mismatch"
    return "approved" if approval_granted(value) else "rejected"


def _admit(request: ToolCallRequest) -> _Admission:
    """判定一次工具调用：是否需要人工批准，以及应当下发的参数。"""
    context = _context_of(request)
    args = _args_of(request)
    contract = effect_of(request.tool)
    if contract.kind not in (ToolEffectKind.NO_LOCAL_EFFECT, ToolEffectKind.DELEGATED_EXECUTION):
        try:
            security = security_context_of(context)
        except RuntimeError:
            return _Admission(False, args, capability_denied="缺少受治理执行身份")
        if contract.kind is ToolEffectKind.SANDBOXED_SHELL and "host_command" not in security.authorization.permissions:
            return _Admission(False, args, capability_denied="host_command")
    policy = policy_from_context(context)

    if args.get("requested_mode") is not None:
        return _admit_escalation(request, policy, context, args, contract)
    if contract.kind in (ToolEffectKind.NO_LOCAL_EFFECT, ToolEffectKind.DELEGATED_EXECUTION):
        return _Admission(False, args)
    if contract.kind is ToolEffectKind.SANDBOXED_SHELL:
        return _Admission(False, args)
    if contract.kind is ToolEffectKind.STRUCTURED_FS:
        return _admit_structured(request, policy, context, args, contract)
    return _admit_opaque(request, policy, args)


def _admit_escalation(
    request: ToolCallRequest, policy: AccessPolicy, context: Mapping[str, Any],
    args: Mapping[str, Any], contract: EffectContract,
) -> _Admission:
    target_mode = validate_escalation(
        policy.mode, str(args["requested_mode"]), str(args.get("reason") or ""),
    )
    binding = bind_call_execution(context, str(request.tool_call.get("id") or ""))
    reads: tuple[Path, ...] = ()
    writes: tuple[Path, ...] = ()
    rewritten = args
    if contract.kind is ToolEffectKind.STRUCTURED_FS:
        resolved = resolve_fs_effect(contract, args, context)
        if not resolved.writes:
            raise ValueError("只有结构化写操作可以申请文件模式放宽")
        if "write" not in security_context_of(context).authorization.permissions:
            return _Admission(False, args, capability_denied="write")
        reads, writes = resolved.reads, resolved.writes
        rewritten = resolved.args if resolved.args is not None else args
        candidate = replace(policy, mode=target_mode)
        denied = tuple(
            path for path in writes
            if decide_path_access(candidate, path, AccessOperation.WRITE) is AccessDecision.DENY
        )
        if denied:
            return _Admission(False, rewritten, reads, writes, denied=denied)
    elif contract.kind is not ToolEffectKind.SANDBOXED_SHELL:
        raise ValueError("此工具不支持文件模式放宽")
    request_id = escalation_request_id(
        binding, str(request.tool_call.get("name") or ""), args, target_mode,
    )
    approval = ApprovalRequest(
        tool=str(request.tool_call.get("name") or ""),
        access_mode=str(policy.mode), cwd=str(binding.workspace),
        agent_role=binding.agent_role, reads=tuple(str(path) for path in reads),
        writes=tuple(str(path) for path in writes), command=_command_of(args),
        requested_mode=str(target_mode), reason=str(args["reason"]).strip(),
        request_id=request_id, run_id=binding.run_id,
        agent_id=binding.agent_id, call_id=binding.call_id,
    )
    return _Admission(True, rewritten, reads, writes, approval, requested_mode=target_mode)


def _admit_structured(
    request: ToolCallRequest,
    policy: AccessPolicy,
    context: Mapping[str, Any],
    args: Mapping[str, Any],
    contract: EffectContract,
) -> _Admission:
    """可结构化枚举的调用：解析全部受治理目标后逐目标判定。"""
    resolved = resolve_fs_effect(contract, args, context)
    rewritten = resolved.args if resolved.args is not None else args
    if resolved.writes and "write" not in security_context_of(context).authorization.permissions:
        return _Admission(False, rewritten, capability_denied="write")
    if resolved.reads and "read" not in security_context_of(context).authorization.permissions:
        return _Admission(False, rewritten, capability_denied="read")
    denied = tuple(
        target for target in resolved.writes
        if decide_path_access(policy, target, AccessOperation.WRITE) is AccessDecision.DENY
    )
    if denied:
        return _Admission(False, rewritten, resolved.reads, resolved.writes, denied=denied)
    pending = [
        *_pending(policy, resolved.reads, AccessOperation.READ),
        *_pending(policy, resolved.writes, AccessOperation.WRITE),
    ]
    if not pending:
        return _Admission(False, rewritten, resolved.reads, resolved.writes)
    return _Admission(
        True, rewritten, resolved.reads, resolved.writes,
        _request(request, policy, resolved.reads, resolved.writes),
    )


def _admit_opaque(
    request: ToolCallRequest, policy: AccessPolicy, args: Mapping[str, Any]
) -> _Admission:
    """不透明效果的调用：受保护模式下整体待决；完全权限下直接执行。"""
    if policy.mode is AccessMode.FULL:
        return _Admission(False, args)
    return _Admission(True, args, request=_request(request, policy, (), ()))


def _pending(
    policy: AccessPolicy, targets: tuple[Path, ...], operation: AccessOperation
) -> list[Path]:
    """挑出需要人工批准的受治理目标。"""
    return [
        target
        for target in targets
        if decide_path_access(policy, target, operation) is AccessDecision.ASK
    ]


def _request(
    request: ToolCallRequest,
    policy: AccessPolicy,
    reads: tuple[Path, ...],
    writes: tuple[Path, ...],
) -> ApprovalRequest:
    """组装批准请求：人在决定前必须看到的全部信息，读目标与写目标分开标注。"""
    context = _context_of(request)
    role = context.get("agent_role")
    return ApprovalRequest(
        tool=str(request.tool_call.get("name") or ""),
        access_mode=str(policy.mode),
        cwd=str(policy.workspace),
        agent_role=str(role) if role else None,
        reads=tuple(str(target) for target in reads),
        writes=tuple(str(target) for target in writes),
        command=_command_of(_args_of(request)),
    )


def _denied(request: ToolCallRequest, admission: _Admission, outcome: str) -> ToolMessage:
    """未获批准：给出可理解的失败结果，使该次执行不是静默跳过。"""
    detail = "、".join(str(target) for target in admission.targets) or "该操作"
    status = {
        "rejected": "用户未批准本次操作",
        "cancelled": "用户取消了本次操作",
        "unavailable": "APPROVAL_UNAVAILABLE: 审批渠道不可用",
        "mismatch": "APPROVAL_MISMATCH: 审批内容与本次调用不一致",
    }.get(outcome, "审批未完成")
    return ToolMessage(
        content=f"{status}，已跳过：{request.tool_call.get('name')} → {detail}",
        tool_call_id=request.tool_call.get("id"),
        status="error",
    )


def _policy_denied(request: ToolCallRequest, admission: _Admission) -> ToolMessage:
    policy = policy_from_context(_context_of(request))
    detail = "、".join(str(target) for target in admission.denied)
    return ToolMessage(
        content=(
            f"FILE_POLICY_DENIED: 当前模式 {policy.mode} 不允许修改 {detail}。"
            "若确需本次放宽，请在原工具调用提供足够的最小 requested_mode 和非空 reason，"
            "由用户单次批准；先核对已完成的副作用，再决定是否重试。"
        ),
        tool_call_id=request.tool_call.get("id"),
        status="error",
    )


def _capability_denied(request: ToolCallRequest, detail: str) -> ToolMessage:
    return ToolMessage(
        content=f"PERMISSION_DENIED: 当前运行未授权 {detail}，已跳过 {request.tool_call.get('name')}",
        tool_call_id=request.tool_call.get("id"),
        status="error",
    )


def _with_args(request: ToolCallRequest, args: Mapping[str, Any]) -> ToolCallRequest:
    """把规范化后的参数下发给工具体；参数未变时原样返回请求。"""
    original = _args_of(request)
    if args == original:
        return request
    return request.override(tool_call={**request.tool_call, "args": dict(args)})


def _with_grant(request: ToolCallRequest, admission: _Admission) -> ToolCallRequest:
    if admission.requested_mode is None or admission.request is None:
        return request
    security = security_context_of(_context_of(request))
    approved = replace(
        security,
        authorization=replace(security.authorization, access_mode=admission.requested_mode),
        extras={
            **security.extras,
            "approved_mode_source": "single-approval",
            "approval_id": admission.request.request_id,
        },
    )
    context = {**request.runtime.context, **approved.to_runtime_context()}
    return request.override(runtime=replace(request.runtime, context=context))


def _context_of(request: ToolCallRequest) -> Mapping[str, Any]:
    context = getattr(getattr(request, "runtime", None), "context", None)
    return context if isinstance(context, Mapping) else {}


def _args_of(request: ToolCallRequest) -> Mapping[str, Any]:
    args = request.tool_call.get("args")
    return args if isinstance(args, Mapping) else {}


def _command_of(args: Mapping[str, Any]) -> str | None:
    command = args.get("command")
    return str(command) if isinstance(command, str) and command.strip() else None
