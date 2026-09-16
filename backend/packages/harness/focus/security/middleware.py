"""本文件对外提供 AccessPolicyMiddleware，作为全部工具执行的唯一准入点。

对外提供:
    AccessPolicyMiddleware — AgentMiddleware 子类：工具执行前判定，待决即交回人类

输入:
    由 LangChain 注入的 ToolCallRequest（含 tool / tool_call / runtime）与下游 handler；
    受治理上下文经 request.runtime.context 提供（workspace、access_mode、agent_role）。

输出:
    工具结果；待决时以中断交回人类，未获批准则返回表示「本次被跳过」的工具错误消息。

具体工作流:
    (1) 读取工具已签发的效果契约；无本地效果与委托执行直接放行
    (2) 可结构化枚举的契约先解析全部受治理本地目标，再逐目标按访问策略判定（读用读、写用写）
    (3) 不透明效果（含全部宿主命令）在受保护模式下整体待决，绝不分析命令文本
    (4) 待决在产生任何副作用之前中断，载荷由 ApprovalRequest 组装，读目标与写目标分开承载
    (5) 获批只作用于当前这一次调用：放行来自中断的恢复值，不建授权表、不设有效期
    (6) 可结构化枚举的调用改用解析器交出的规范化参数下发，使判定目标与执行目标同一
    (7) Loop workspace lease 存在时，每次工具调用前重新验证 fencing token
    (8) 未获批准返回工具错误消息，使该次执行得到可理解的结果而非静默跳过

示例:
    middlewares = [AccessPolicyMiddleware(), *其他中间件]
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command, interrupt

from focus.security.approval import ApprovalRequest, approval_granted
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
        if admission.asked and not _approved(admission):
            return _denied(request, admission)
        return handler(_with_args(request, admission.args))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolResult]],
    ) -> ToolResult:
        await _assert_workspace_lease(request)
        admission = _admit(request)
        if admission.asked and not _approved(admission):
            return _denied(request, admission)
        return await handler(_with_args(request, admission.args))


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


def _approved(admission: _Admission) -> bool:
    """向人类请求批准；放行是本次中断的恢复值，因此天然只作用于当前这一次调用。"""
    if admission.request is None:
        return False
    return approval_granted(interrupt(admission.request.payload()))


def _admit(request: ToolCallRequest) -> _Admission:
    """判定一次工具调用：是否需要人工批准，以及应当下发的参数。"""
    context = _context_of(request)
    policy = policy_from_context(context)
    args = _args_of(request)
    contract = effect_of(request.tool)

    if contract.kind in (ToolEffectKind.NO_LOCAL_EFFECT, ToolEffectKind.DELEGATED_EXECUTION):
        return _Admission(False, args)
    if contract.kind is ToolEffectKind.STRUCTURED_FS:
        return _admit_structured(request, policy, context, args, contract)
    return _admit_opaque(request, policy, args)


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


def _denied(request: ToolCallRequest, admission: _Admission) -> ToolMessage:
    """未获批准：给出可理解的失败结果，使该次执行不是静默跳过。"""
    detail = "、".join(str(target) for target in admission.targets) or "该操作"
    return ToolMessage(
        content=f"用户未批准本次越界操作，已跳过：{request.tool_call.get('name')} → {detail}",
        tool_call_id=request.tool_call.get("id"),
        status="error",
    )


def _with_args(request: ToolCallRequest, args: Mapping[str, Any]) -> ToolCallRequest:
    """把规范化后的参数下发给工具体；参数未变时原样返回请求。"""
    original = _args_of(request)
    if args == original:
        return request
    return request.override(tool_call={**request.tool_call, "args": dict(args)})


def _context_of(request: ToolCallRequest) -> Mapping[str, Any]:
    context = getattr(getattr(request, "runtime", None), "context", None)
    return context if isinstance(context, Mapping) else {}


def _args_of(request: ToolCallRequest) -> Mapping[str, Any]:
    args = request.tool_call.get("args")
    return args if isinstance(args, Mapping) else {}


def _command_of(args: Mapping[str, Any]) -> str | None:
    command = args.get("command")
    return str(command) if isinstance(command, str) and command.strip() else None
