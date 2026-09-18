"""本文件对外提供执行身份、准入身份与路由身份的类型与唯一派生，是「这次执行是谁、能做什么」的唯一归属地。

对外提供:
    AuthorizationIdentity — 准入身份：工作根、权柄面、能力权限、访问模式、执行主体角色
    RoutingIdentity — 路由身份：会话、工作区、执行主体、所属任务、执行命名空间
    ExecutionProfile — 服务端拥有的执行身份档案（两类身份 + 归属 + 模型），启动点只提供它
    SecurityContext — 一次执行的受治理安全上下文
    ChildRole — 允许从父级单调派生的内联执行角色
    RUNTIME_CONTEXT_KEYS — 本模块投影到运行上下文的全部扁平键（生产者声明与投影实现共用）
    derive_security_context(profile) — 唯一派生：由档案算出安全上下文
    derive_child_security_context(parent, role, ...) — 内联执行的单调派生
    security_context_of(context) — 从运行上下文取回安全上下文；缺失即失败
    SECURITY_CONTEXT_KEY — 承载安全上下文的保留键

输入:
    profile: ExecutionProfile — 由启动点从服务端持久化状态构造，字段不由调用方提供
    parent: SecurityContext — 父级已认证的安全上下文
    role: ChildRole — 内联子执行的角色
    context: object — 运行上下文（扁平字典）

输出:
    SecurityContext — 含 authorization 与 routing 两类身份；owner 为 None 表示显式无归属
    security_context_of → SecurityContext；缺失或类型不符时抛 RuntimeError
    SecurityContext.to_runtime_context() → dict — 扁平投影，供既有读取点使用

具体工作流:
    (1) 档案是唯一可信来源：受治理字段一律由 derive_security_context 从档案算出，
        启动点不得自行拼装能力权限、访问模式、工作根或执行命名空间
    (2) 派生同时做归一与自检：工作区必须为绝对路径，工作根必须包含工作区，重复根被去重
    (3) 内联子执行只能单调派生：能力权限不超集、工作根不超集、访问模式不更宽、权柄面不放宽；
        请求越界即失败，而不是静默收窄
    (4) 运行上下文保持扁平字典（既有读取点不动），保留键承载本对象；扁平键是它的机械投影
    (5) 缺失合法安全上下文一律失败，绝不以宽松默认值继续执行

示例:
    context = derive_security_context(profile).to_runtime_context()
    child = derive_child_security_context(security_context_of(context), ChildRole.SPAWN_AGENT)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from focus.security.authority import authority_surfaces
from focus.security.governed import declare_governed_keys, declare_governed_producer
from focus.security.paths import is_within
from focus.security.policy import AccessMode, AccessPolicy

SECURITY_CONTEXT_KEY = "security_context"
"""运行上下文里承载 SecurityContext 的保留键。"""

RUNTIME_CONTEXT_KEYS: tuple[str, ...] = (
    SECURITY_CONTEXT_KEY,
    "workspace",
    "permissions",
    "access_mode",
    "agent_role",
    "thread_id",
    "workspace_id",
    "agent_id",
    "task_id",
    "checkpoint_ns",
    "run_id",
    "user_id",
    "model_name",
)
"""本模块投影到运行上下文的全部扁平键；生产者声明与投影实现共用它，避免两处漂移。"""

# 本模块是这些扁平键的唯一生产者：它们全部由 to_runtime_context 机械投影产生
declare_governed_keys(*RUNTIME_CONTEXT_KEYS)
for _key in RUNTIME_CONTEXT_KEYS:
    declare_governed_producer(_key, "focus.security.context.to_runtime_context")

_EMPTY = ()


class ChildRole(StrEnum):
    """允许从父级安全上下文单调派生的内联执行角色。"""

    SPAWN_AGENT = "spawn_agent"
    COMMITMENT_WORKER = "commitment_worker"


@dataclass(frozen=True)
class AuthorizationIdentity:
    """准入身份：决定这次执行能把能力作用到哪里。"""

    workspace: Path
    roots: tuple[Path, ...]
    permissions: tuple[str, ...]
    access_mode: AccessMode
    agent_role: str
    authority: tuple[str, ...] = _EMPTY


@dataclass(frozen=True)
class RoutingIdentity:
    """路由身份：决定事件归属、数据作用域与检查点空间。

    task_id 是执行所属任务（会话数据的作用域判据），与 thread_id / workspace_id 同源——都来自
    服务端登记记录，因此不由调用方载荷供给。
    run_id 是本次运行的标识（事件归属与用量记账用它），默认空值只为便于测试构造；
    受支持的启动路径都从服务端记录显式传入。
    """

    thread_id: str
    workspace_id: str
    agent_id: str
    task_id: str
    checkpoint_ns: str
    run_id: str = ""


@dataclass(frozen=True)
class ExecutionProfile:
    """服务端拥有的执行身份档案；启动点只负责提供它。"""

    authorization: AuthorizationIdentity
    routing: RoutingIdentity
    owner: str | None = None
    model_name: str | None = None


@dataclass(frozen=True)
class SecurityContext:
    """一次执行的受治理安全上下文。"""

    authorization: AuthorizationIdentity
    routing: RoutingIdentity
    owner: str | None = None
    model_name: str | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)

    def to_runtime_context(self) -> dict[str, Any]:
        """把安全上下文机械投影为扁平运行上下文。

        扁平键只允许从这里产生；任何启动点直接写这些键都会造成第二个来源。
        """
        context: dict[str, Any] = {
            SECURITY_CONTEXT_KEY: self,
            "workspace": str(self.authorization.workspace),
            "permissions": list(self.authorization.permissions),
            "access_mode": str(self.authorization.access_mode),
            "agent_role": self.authorization.agent_role,
            "thread_id": self.routing.thread_id,
            "workspace_id": self.routing.workspace_id,
            "agent_id": self.routing.agent_id,
            "task_id": self.routing.task_id,
            "checkpoint_ns": self.routing.checkpoint_ns,
            "run_id": self.routing.run_id,
        }
        if self.owner is not None:
            context["user_id"] = self.owner
        if self.model_name is not None:
            context["model_name"] = self.model_name
        context.update(self.extras)
        return context

    def policy(self) -> AccessPolicy:
        """把准入身份投影为访问策略；准入判定只认这一个策略。"""
        return AccessPolicy(
            mode=self.authorization.access_mode,
            workspace=self.authorization.workspace,
            roots=self.authorization.roots,
            authority=authority_surfaces(self.authorization.workspace),
        )


def derive_security_context(profile: ExecutionProfile) -> SecurityContext:
    """唯一派生：由执行身份档案算出安全上下文，并完成归一与自检。"""
    authorization = _normalize(profile.authorization)
    return SecurityContext(
        authorization=authorization,
        routing=profile.routing,
        owner=profile.owner,
        model_name=profile.model_name,
    )


def derive_child_security_context(
    parent: SecurityContext,
    role: ChildRole,
    *,
    permissions: Iterable[str] | None = None,
    roots: Iterable[Path | str] | None = None,
    access_mode: AccessMode | None = None,
    authority: Iterable[str] | None = None,
) -> SecurityContext:
    """内联子执行的单调派生：只能收窄，任何放宽都失败。

    未提供某项时按父级原值继承；内联执行没有自己的执行命名空间，因此路由身份沿用父级。
    """
    authorization = replace(
        parent.authorization,
        permissions=_tuple(permissions) if permissions is not None else parent.authorization.permissions,
        roots=tuple(Path(root) for root in roots) if roots is not None else parent.authorization.roots,
        access_mode=access_mode or parent.authorization.access_mode,
        authority=_tuple(authority) if authority is not None else parent.authorization.authority,
        agent_role=str(role),
    )
    child = replace(parent, authorization=authorization)
    _require_not_wider(child, parent)
    return child


def security_context_of(context: object) -> SecurityContext:
    """从运行上下文取回安全上下文；缺失或类型不符即失败。"""
    if not isinstance(context, Mapping):
        raise RuntimeError("缺少受治理安全上下文: 运行上下文必须是字典")
    value = context.get(SECURITY_CONTEXT_KEY)
    if not isinstance(value, SecurityContext):
        raise RuntimeError(
            "缺少受治理安全上下文: runtime.context 未携带 SecurityContext，"
            "执行必须由服务端派生的安全上下文驱动"
        )
    return value


def has_security_context(context: object) -> bool:
    """运行上下文是否已携带安全上下文。"""
    return isinstance(context, Mapping) and isinstance(
        context.get(SECURITY_CONTEXT_KEY), SecurityContext
    )


def _normalize(authorization: AuthorizationIdentity) -> AuthorizationIdentity:
    """归一准入身份：工作区必须绝对，工作根去重且以工作区打头。"""
    workspace = Path(authorization.workspace)
    if not workspace.is_absolute():
        raise ValueError(f"执行身份的工作区必须是绝对路径: {workspace}")
    roots = tuple(dict.fromkeys([workspace, *(Path(root) for root in authorization.roots)]))
    if not all(root.is_absolute() for root in roots):
        raise ValueError("执行身份的工作根必须都是绝对路径")
    return replace(
        authorization,
        workspace=workspace,
        roots=roots,
        permissions=_tuple(authorization.permissions),
        authority=_tuple(authorization.authority),
    )


def _require_not_wider(child: SecurityContext, parent: SecurityContext) -> None:
    """单调性自检：权限与权柄面不得超集，工作根不得越出父级，访问模式不得更宽。"""
    child_auth = child.authorization
    parent_auth = parent.authorization
    if not set(child_auth.permissions) <= set(parent_auth.permissions):
        raise ValueError("内联子执行的能力权限不得超出父级")
    for root in child_auth.roots:
        if not any(is_within(parent_root, root) for parent_root in parent_auth.roots):
            raise ValueError("内联子执行的工作根不得超出父级")
    if not set(child_auth.authority) <= set(parent_auth.authority):
        raise ValueError("内联子执行的权柄面判定不得放宽")
    if _mode_rank(child_auth.access_mode) > _mode_rank(parent_auth.access_mode):
        raise ValueError("内联子执行的访问模式不得宽于父级")


def _mode_rank(mode: AccessMode) -> int:
    """访问模式的宽窄序：工作区保护 < 本机完全权限。"""
    return 1 if mode is AccessMode.FULL else 0


def _tuple(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values))
