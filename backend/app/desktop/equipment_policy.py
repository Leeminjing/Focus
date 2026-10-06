"""本文件对外提供 effective_equipment 与 activation_equipment 装备解析函数。

输入为服务端可信来源、显式 override 与可选权限缩减；输出为复制后的完整有效装备。
具体工作流为仅继承公开装备字段、拒绝扩大来源权限、规范化 access_mode，再应用角色只读限制。
激活可显式沿用初始 Run，旧客户端仍使用其显式装备；来源缺失时拒绝隐式继承。
示例：activation_equipment(run.equipment, {}, (), inherit=True) 保留该 Run 的模型、技能与权限。
"""

from copy import deepcopy
from typing import Any, Mapping, Sequence

from focus.security.policy import AccessMode

_FIELDS = ("model_name", "patrol_model_name", "curator_model_name", "verifier_model_name", "skills", "skill_snapshots", "memory_ids", "permissions", "access_mode")


def effective_equipment(
    source: Mapping[str, Any],
    *,
    overrides: Mapping[str, Any] | None = None,
    permissions: Sequence[str] | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    result = {key: deepcopy(source[key]) for key in _FIELDS if key in source}
    result.update({key: deepcopy(value) for key, value in (overrides or {}).items() if key in _FIELDS})
    available = set(source.get("permissions") or ())
    selected = list(permissions if permissions is not None else result.get("permissions") or ())
    if not set(selected).issubset(available):
        raise ValueError("装备权限超出可信来源")
    result["permissions"] = [item for item in dict.fromkeys(selected) if not read_only or item == "read"]
    result["access_mode"] = str(AccessMode(result.get("access_mode") or "read-only"))
    if read_only:
        result["access_mode"] = str(AccessMode.READ_ONLY)
    result["skills"] = list(result.get("skills") or ())
    result["skill_snapshots"] = list(result.get("skill_snapshots") or ())
    return result


def activation_equipment(
    initial: Mapping[str, Any],
    requested: Mapping[str, Any],
    scope: Sequence[str],
    *,
    inherit: bool,
) -> dict[str, Any]:
    source = initial if inherit else requested
    if inherit and (not initial.get("permissions") or not initial.get("access_mode")):
        raise ValueError("初始 Run 缺少可继承的权限或 access_mode")
    if not inherit:
        source = {**requested, "permissions": requested.get("permissions") or list(scope)}
    execution_scope = tuple(item for item in scope if item in {"read", "write", "host_command"})
    resolved = effective_equipment(source, overrides=requested, permissions=execution_scope or source.get("permissions") or ())
    resolved.setdefault("patrol_model_name", resolved.get("model_name"))
    return resolved
