"""本文件对外提供本地访问策略的类型、唯一准入判定与策略构造，是「能在哪里做」的唯一归属地。

对外提供:
    AccessMode — 访问模式：WORKSPACE（工作区保护）/ FULL（本机完全权限）
    AccessOperation — 受治理操作类型：READ / WRITE
    AccessDecision — 判定结果：ALLOW（直接执行）/ ASK（交回人类批准）
    AccessPolicy — 一次执行的访问策略（模式 + 相对解析基准 + 全部工作根）
    decide_path_access(policy, target, operation) — 唯一准入判定
    policy_from_context(context) — 由运行上下文构造访问策略

输入:
    policy: AccessPolicy — 由运行起点派生的访问策略
    target: Path — 已经规范化的真实宿主路径
    operation: AccessOperation — 该次操作是读还是写
    context: dict | None — 运行上下文，读取其中的 workspace 与 access_mode

输出:
    decide_path_access → AccessDecision（只有放行与待决两种，不存在拒绝）
    policy_from_context → AccessPolicy；access_mode 缺失或无法识别时按最严的 WORKSPACE

具体工作流:
    (1) FULL 模式恒放行——完全权限只解除本机资源准入，不改变能力权限，也不解除其它硬协议
    (2) WORKSPACE 模式下，目标位于任一工作根之内即放行，否则待决
    (3) 判定恒不返回拒绝：越界的终点是交回人类，而不是终止该次执行
    (4) 读与写当前同解；权柄面引入后，同一目标的两种操作可以得出不同结论
    (5) 策略构造只认服务端提供的上下文；access_mode 无法识别时按最严处理

示例:
    policy = policy_from_context(runtime.context)
    decide_path_access(policy, target, AccessOperation.WRITE)   # → AccessDecision.ALLOW
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from focus.security.authority import (
    AuthoritySurface,
    authority_surface_for,
    authority_surfaces,
)
from focus.security.governed import declare_governed_keys
from focus.security.paths import is_within

logger = logging.getLogger(__name__)

# 准入判定读取的扁平回退字段：它们参与安全决策，因此属于受治理键
declare_governed_keys("workspace", "access_mode", "allow_global_config")


class AccessMode(StrEnum):
    """访问模式：决定 Agent 能把已有的能力作用到哪里。"""

    WORKSPACE = "workspace"
    FULL = "full"


class AccessOperation(StrEnum):
    """受治理操作类型。"""

    READ = "read"
    WRITE = "write"


class AccessDecision(StrEnum):
    """准入判定结果：放行，或交回人类批准。"""

    ALLOW = "allow"
    ASK = "ask"


@dataclass(frozen=True)
class AccessPolicy:
    """一次执行的访问策略。

    workspace 是相对路径的解析基准，也是宿主命令的执行目录；roots 恒包含 workspace；
    authority 是权柄面集合（受保护模式下优先于工作根）。
    """

    mode: AccessMode
    workspace: Path
    roots: tuple[Path, ...]
    authority: tuple[AuthoritySurface, ...] = ()


def decide_path_access(policy: AccessPolicy, target: Path, operation: AccessOperation) -> AccessDecision:
    """唯一准入判定：回答这个真实路径在当前策略下是放行还是交回人类。

    判定顺序是「模式 → 权柄面 → 工作根」：完全权限整体不适用本机资源准入；受保护模式下
    权柄面优先于工作根（工作区本身可能就是应用仓库，此时应用配置仍须逐次批准）；
    其余目标按是否落在工作根内决定。
    """
    if policy.mode is AccessMode.FULL:
        return AccessDecision.ALLOW
    surface = authority_surface_for(target, policy.authority)
    if surface is not None:
        if operation is AccessOperation.WRITE:
            return AccessDecision.ASK
        return AccessDecision.ALLOW if surface.readable else AccessDecision.ASK
    if any(is_within(root, target) for root in policy.roots):
        return AccessDecision.ALLOW
    return AccessDecision.ASK


def policy_from_context(context: object) -> AccessPolicy:
    """由运行上下文构造访问策略。

    已携带受治理安全上下文时以它为准（工作根与访问模式都来自执行身份档案）；否则回退到
    扁平的 workspace / access_mode 字段，缺少工作区即失败，模式无法识别即按最严处理。
    """
    from focus.security.context import has_security_context, security_context_of

    if has_security_context(context):
        return security_context_of(context).policy()
    values = context if isinstance(context, dict) else {}
    workspace_value = values.get("workspace")
    if not workspace_value:
        raise RuntimeError("缺少工作区上下文: runtime.context['workspace']")
    workspace = Path(str(workspace_value)).resolve()
    return AccessPolicy(
        mode=_access_mode(values),
        workspace=workspace,
        roots=_workspace_roots(workspace, values),
        authority=authority_surfaces(workspace),
    )


def restrictive_policy(workspace: Path) -> AccessPolicy:
    """构造最严的访问策略：工作区保护模式，且只有工作区一个工作根。

    用于受治理上下文不可得的调用方（界面侧）与未声明访问模式的执行：宁可逐次待决，
    也不放大到完全权限。
    """
    resolved = Path(workspace).resolve()
    return AccessPolicy(
        mode=AccessMode.WORKSPACE,
        workspace=resolved,
        roots=(resolved,),
        authority=authority_surfaces(resolved),
    )


def workspace_roots(workspace: Path, *, allow_global_config: bool = False) -> tuple[Path, ...]:
    """汇总全部工作根：主工作区在前；明确允许时并入全局配置家目录。"""
    roots = [Path(workspace).resolve()]
    if allow_global_config:
        from focus.config.layered import global_home

        roots.append(global_home().resolve())
    return tuple(dict.fromkeys(roots))


def _access_mode(values: dict) -> AccessMode:
    """解析访问模式；缺失或无法识别一律按最严的工作区保护处理并留下可见告警。"""
    raw = values.get("access_mode")
    if not raw:
        return AccessMode.WORKSPACE
    try:
        return AccessMode(str(raw))
    except ValueError:
        logger.warning("无法识别的访问模式 %r，已按最严的工作区保护处理", raw)
        return AccessMode.WORKSPACE


def _workspace_roots(workspace: Path, values: dict) -> tuple[Path, ...]:
    """扁平回退路径的工作根：与装配层共用同一份构造。"""
    return workspace_roots(workspace, allow_global_config=bool(values.get("allow_global_config")))
