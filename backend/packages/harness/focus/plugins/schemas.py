"""
本文件对外提供插件清单与插件声明的数据模型及声明校验函数。

对外提供:
    PluginManifest(BaseModel) — plugin.json 的声明式清单（name/version/enabled/provides/requires/entry/runtime）
    PluginRuntime(BaseModel) — 进程外插件启动声明（command/args），与 entry 二选一
    PluginContext — 构建插件时传给 build_plugin 的上下文（仅插件自有数据）
    PluginDeclaration — build_plugin 返回的声明式实现集合（tools/hooks/services）
    validate_declaration(manifest, declaration, catalog) — 校验声明兑现清单，返回不兼容接口名列表
    validate_requires(manifest, catalog) — 校验 requires 均为目录内 service 接口

输入:
    PluginManifest 字段:
        name: str — 插件名（SHALL 与目录名一致）
        version: str — 插件版本
        enabled: bool — 是否启用（False 不加载）
        provides: list[str] — 声明提供的接口名列表
        requires: list[str] — 依赖的 service 接口名列表（按接口依赖，不按具体插件）
        entry: str — 进程内插件入口文件名，暴露 build_plugin(context)
        runtime: PluginRuntime | None — 进程外插件启动命令（command/args），与 entry 互斥

输出:
    validate_declaration → list[str] 不兼容接口名（空列表表示全部兑现）；
    validate_requires → list[str] 非法依赖接口名（空列表表示全部合法）

具体工作流:
    (1) validate_declaration: provides 中每个接口按类别校验实现存在——
        "tool" 要求 tools 非空，"hook.X" 要求 hooks[X] 非空，"service.X" 要求 services[X] 存在
    (2) validate_requires: requires 中每个接口必须在目录内且类别为 service
    (3) PluginDeclaration 的 hooks 值为"回调列表"，services 值为"实现对象"

示例:
    manifest = PluginManifest(name="vision", provides=["tool", "hook.before_model"])
    declaration = PluginDeclaration(tools=[browser_tool], hooks={"hook.before_model": [sanitize]})
    validate_declaration(manifest, declaration, catalog)  # []
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from focus.plugins.interfaces import InterfaceCatalog, InterfaceKind


class PluginRuntime(BaseModel):
    """进程外插件声明：系统只 spawn 声明的命令，不准备任何运行环境。"""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    enabled: bool = True
    provides: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    entry: str = "plugin.py"
    runtime: PluginRuntime | None = None

    @model_validator(mode="after")
    def _entry_xor_runtime(self) -> "PluginManifest":
        if self.runtime is not None and self.entry != "plugin.py":
            raise ValueError("runtime 与 entry 只能二选一")
        return self


@dataclass(frozen=True)
class PluginContext:
    """构建插件时系统提供的上下文：仅插件自有数据，系统不解释其含义。"""

    config: dict[str, Any]  # plugins/<name>/config.json 原样内容（无该文件时为空 dict）
    plugin_dir: Path


@dataclass
class PluginDeclaration:
    """build_plugin 的返回产物：插件声明的实现集合。"""

    tools: list[Any] = field(default_factory=list)
    hooks: dict[str, list[Callable]] = field(default_factory=dict)
    services: dict[str, Any] = field(default_factory=dict)


def validate_declaration(
    manifest: PluginManifest,
    declaration: PluginDeclaration,
    catalog: InterfaceCatalog,
) -> list[str]:
    incompatible: list[str] = []
    for name in manifest.provides:
        iface = catalog.get(name)
        if iface is None:
            incompatible.append(name)
            continue
        if iface.kind is InterfaceKind.TOOL and not declaration.tools:
            incompatible.append(name)
        elif iface.kind is InterfaceKind.HOOK and not declaration.hooks.get(name):
            incompatible.append(name)
        elif iface.kind is InterfaceKind.SERVICE and name not in declaration.services:
            incompatible.append(name)
    return incompatible


def validate_requires(manifest: PluginManifest, catalog: InterfaceCatalog) -> list[str]:
    invalid: list[str] = []
    for name in manifest.requires:
        iface = catalog.get(name)
        if iface is None or iface.kind is not InterfaceKind.SERVICE:
            invalid.append(name)
    return invalid
