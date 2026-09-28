"""本文件对外提供三档文件执行模式、路径策略判定及运行上下文策略构造。

输入为服务端认可的工作区、访问模式、真实目标、读写操作与附加业务权柄面。
输出为 AccessPolicy 以及 ALLOW、ASK 或 DENY 判定；旧模式只在读取历史值时转换。
具体工作流为先解析模式并保守回退只读，再按文件模式限定写入边界，最后应用敏感路径
业务审批；完全访问只解除文件沙箱限制，不补发工具能力。
示例：decide_path_access(policy, target, AccessOperation.WRITE) 返回 AccessDecision.DENY。
"""

from __future__ import annotations

import logging
import tempfile
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
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"
    WORKSPACE = "workspace-write"
    FULL = "danger-full-access"

    @classmethod
    def _missing_(cls, value: object) -> AccessMode | None:
        return {"workspace": cls.WORKSPACE_WRITE, "full": cls.DANGER_FULL_ACCESS}.get(value)


class AccessOperation(StrEnum):
    """受治理操作类型。"""

    READ = "read"
    WRITE = "write"


class AccessDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


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
    if operation is AccessOperation.WRITE:
        if policy.mode is AccessMode.READ_ONLY:
            return AccessDecision.DENY
        if policy.mode is AccessMode.WORKSPACE_WRITE and not any(
            is_within(root, target) for root in policy.roots
        ):
            return AccessDecision.DENY
    surface = authority_surface_for(target, policy.authority)
    if surface is not None:
        if operation is AccessOperation.WRITE:
            return AccessDecision.ASK
        return AccessDecision.ALLOW if surface.readable else AccessDecision.ASK
    return AccessDecision.ALLOW


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
    resolved = Path(workspace).resolve()
    return AccessPolicy(
        mode=AccessMode.READ_ONLY,
        workspace=resolved,
        roots=(resolved,),
        authority=authority_surfaces(resolved),
    )


def workspace_roots(workspace: Path, *, allow_global_config: bool = False) -> tuple[Path, ...]:
    """汇总结构化文件工具可写根：工作区及共享平台临时区。"""
    roots = [Path(workspace).resolve(), Path(tempfile.gettempdir()).resolve()]
    if allow_global_config:
        from focus.config.layered import global_home

        roots.append(global_home().resolve())
    return tuple(dict.fromkeys(roots))


def _access_mode(values: dict) -> AccessMode:
    raw = values.get("access_mode")
    if not raw:
        return AccessMode.READ_ONLY
    try:
        return AccessMode(str(raw))
    except ValueError:
        logger.warning("无法识别的访问模式 %r，已按只读模式处理", raw)
        return AccessMode.READ_ONLY


def _workspace_roots(workspace: Path, values: dict) -> tuple[Path, ...]:
    """扁平回退路径的工作根：与装配层共用同一份构造。"""
    return workspace_roots(workspace, allow_global_config=bool(values.get("allow_global_config")))
