"""
本文件对外提供插件接口目录 InterfaceCatalog 与 v1 内置接口定义。

对外提供:
    InterfaceKind(StrEnum) — 接口类别: tool / hook / service
    PluginInterface — 单个插件接口的静态属性声明（frozen dataclass）
    InterfaceCatalog — 系统静态接口目录，插件只能声明目录内接口
    builtin_catalog() — 构造 v1 内置接口目录

输入:
    PluginInterface 字段:
        name: str — 接口名（"tool" / "hook.<name>" / "service.<name>"）
        cardinality: str — "multi"（允许多实现加法）或 "single"（仅一个当前实现）
        mutability: str — "mutable"（可修改开放数据）/ "read-only"（只读观察）
        failure_policy: str — 实现失败时的策略: "skip"（跳过继续）/ "abort"（终止操作）
        timeout_seconds: float | None — 单次实现调用的超时上限，None 表示不超时

输出:
    builtin_catalog() → InterfaceCatalog；InterfaceCatalog.get(name) → PluginInterface | None

具体工作流:
    (1) 接口类别由接口名推导: "tool" → tool，"hook." 前缀 → hook，"service." 前缀 → service
    (2) v1 内置目录: tool + 六个生命周期 hook（before_agent/before_model 可变，
        after_model/after_agent/before_tool/after_tool 中 before_tool 可变、
        after_model/after_agent/after_tool 只读）；service 接口目录为空，
        未来新增服务接口只需在本表加一行
    (3) 插件清单中目录之外的接口名 → 拒绝接入（Unsupported Extension Interface）

示例:
    catalog = builtin_catalog()
    iface = catalog.get("hook.before_model")
    iface.timeout_seconds  # 10.0
"""

from dataclasses import dataclass
from enum import StrEnum


class InterfaceKind(StrEnum):
    TOOL = "tool"
    HOOK = "hook"
    SERVICE = "service"


@dataclass(frozen=True)
class PluginInterface:
    name: str
    cardinality: str  # multi | single
    mutability: str  # mutable | read-only
    failure_policy: str = "skip"  # skip | abort
    timeout_seconds: float | None = None

    @property
    def kind(self) -> InterfaceKind:
        return kind_of(self.name)


def kind_of(name: str) -> InterfaceKind:
    if name == InterfaceKind.TOOL.value:
        return InterfaceKind.TOOL
    if name.startswith("hook."):
        return InterfaceKind.HOOK
    if name.startswith("service."):
        return InterfaceKind.SERVICE
    raise ValueError(f"接口名无法归类: {name!r}")


_BUILTINS: tuple[PluginInterface, ...] = (
    PluginInterface(name="tool", cardinality="multi", mutability="mutable"),
    PluginInterface(name="hook.before_agent", cardinality="multi", mutability="mutable",
                    failure_policy="skip", timeout_seconds=10.0),
    PluginInterface(name="hook.before_model", cardinality="multi", mutability="mutable",
                    failure_policy="skip", timeout_seconds=10.0),
    PluginInterface(name="hook.after_model", cardinality="multi", mutability="read-only",
                    failure_policy="skip", timeout_seconds=5.0),
    PluginInterface(name="hook.after_agent", cardinality="multi", mutability="read-only",
                    failure_policy="skip", timeout_seconds=5.0),
    PluginInterface(name="hook.before_tool", cardinality="multi", mutability="mutable",
                    failure_policy="skip", timeout_seconds=5.0),
    PluginInterface(name="hook.after_tool", cardinality="multi", mutability="read-only",
                    failure_policy="skip", timeout_seconds=5.0),
)

# ponytail: v1 服务接口目录为空；新增服务接口（含 single 仲裁接口）只需在 _BUILTINS 加一行
_SERVICES: tuple[PluginInterface, ...] = ()


class InterfaceCatalog:
    """系统静态接口目录：插件只能声明目录内接口。"""

    def __init__(self, interfaces: tuple[PluginInterface, ...] = ()) -> None:
        self._interfaces = {iface.name: iface for iface in interfaces}

    def get(self, name: str) -> PluginInterface | None:
        return self._interfaces.get(name)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._interfaces)


def builtin_catalog() -> InterfaceCatalog:
    return InterfaceCatalog((*_BUILTINS, *_SERVICES))
