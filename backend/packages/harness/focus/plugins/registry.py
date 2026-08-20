"""
本文件对外提供 PluginRegistry 依赖注入注册表与 PluginRecord/HookImpl/TraceEvent 数据类。

对外提供:
    PluginRecord — 单个插件的接入档案（状态/理由/注入清单/冲突）
    HookImpl — hook 接口的一个插件实现（插件名 + 回调）
    PluginRegistry — 接口 → 有序实现注册表；注入原子性、单实现冲突拒绝、依赖解析、
        撤销语义（不登记即不生效）、trace 环形缓冲

输入:
    PluginRegistry(catalog: InterfaceCatalog, trace_maxlen: int = 500)
    register(manifest, declaration) → PluginRecord — 校验声明后登记（pending，未提交）
    register_remote(manifest, plugin_dir) → PluginRecord — 进程外插件 spawn/握手后按同构声明登记
    resolve_dependencies() → None — 两阶段加载的收尾：解析 requires，提交 Active 插件实现

输出:
    tools() → list[BaseTool]；hooks(name) → list[HookImpl]（注册表稳定顺序）；
    service(name) → 实现 | list[实现]（按接口 cardinality）；
    trace(...) → None；traces() → list[dict]（最新在前）；list_plugins() → list[dict]

具体工作流:
    (1) register: 声明校验（provides 未知接口 → Rejected: Unsupported Extension Interface；
        声明与实现不符 → Rejected: 接口不符）→ single 服务接口冲突检测（已存在实现 →
        Rejected: Injection Conflict，展示 Interface/Current/New）→ 通过后挂入待提交队列
    (2) resolve_dependencies: 按登记顺序逐个解析 requires（只按接口名，任意实现满足）——
        缺失 → Unavailable: Missing dependency；满足 → 提交全部实现（注入原子性）并置 Active
    (3) 稳定顺序 = 登记顺序 = 插件目录名字典序 + 声明序；撤销 = 不登记/不提交，
        运行中的 run 使用装配时快照
    (4) trace 为常开环形缓冲，满则淘汰最旧

示例:
    registry = PluginRegistry(builtin_catalog())
    record = registry.register(manifest, declaration)
    registry.resolve_dependencies()
    tools = registry.tools()
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from focus.plugins.interfaces import InterfaceCatalog, InterfaceKind
from focus.plugins.schemas import (
    PluginDeclaration,
    PluginManifest,
    validate_declaration,
    validate_requires,
)

logger = logging.getLogger(__name__)


@dataclass
class PluginRecord:
    manifest: PluginManifest
    status: str  # pending | active | unavailable | rejected
    reason: str | None = None
    injected: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    conflict: dict[str, str] | None = None


@dataclass(frozen=True)
class HookImpl:
    plugin: str
    fn: Callable


@dataclass(frozen=True)
class TraceEvent:
    interface: str
    plugin: str
    status: str  # success | failed | timeout
    duration_ms: float
    error: str | None = None
    at: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PluginRegistry:
    def __init__(self, catalog: InterfaceCatalog, trace_maxlen: int = 500) -> None:
        self.catalog = catalog
        self._records: list[PluginRecord] = []
        self._pending: list[tuple[PluginRecord, PluginDeclaration]] = []
        self._tools: list[tuple[str, Any]] = []
        self._hooks: dict[str, list[HookImpl]] = {}
        self._services: dict[str, list[tuple[str, Any]]] = {}
        # f18: 插件声明的桌面 API 路由与前端资源(仅 Active 插件在挂载时生效)
        self._assets: dict[str, dict[str, Any]] = {}
        self._traces: deque[dict] = deque(maxlen=trace_maxlen)

    def register_assets(self, name: str, assets: dict[str, Any]) -> None:
        """登记插件的桌面 API 路由与前端资源;挂载时仅 Active 插件生效。"""
        self._assets[name] = assets

    def active_assets(self) -> dict[str, dict[str, Any]]:
        """返回 Active 插件已登记的资源(router / assets_dir),供系统挂载。"""
        active = {
            record.manifest.name
            for record in self._records
            if record.status == "active"
        }
        return {
            name: assets
            for name, assets in self._assets.items()
            if name in active
        }

    def register(self, manifest: PluginManifest, declaration: PluginDeclaration) -> PluginRecord:
        invalid_requires = validate_requires(manifest, self.catalog)
        if invalid_requires:
            # 依赖接口未定义或非 service：按依赖缺失语义记录为 Unavailable（要求 9）
            record = PluginRecord(
                manifest=manifest, status="unavailable", missing=invalid_requires,
                reason="Missing dependency: " + ", ".join(invalid_requires),
            )
            self._records.append(record)
            return record
        reason, conflict = self._check(manifest, declaration)
        if reason is not None:
            record = PluginRecord(manifest=manifest, status="rejected", reason=reason, conflict=conflict)
        else:
            record = PluginRecord(manifest=manifest, status="pending")
            self._pending.append((record, declaration))
        self._records.append(record)
        return record

    def _check(
        self, manifest: PluginManifest, declaration: PluginDeclaration,
    ) -> tuple[str | None, dict[str, str] | None]:
        unknown = [name for name in manifest.provides if self.catalog.get(name) is None]
        if unknown:
            return "Unsupported Extension Interface: " + ", ".join(unknown), None
        incompatible = validate_declaration(manifest, declaration, self.catalog)
        if incompatible:
            return "接口不符: " + ", ".join(incompatible), None
        for name in manifest.provides:
            iface = self.catalog.get(name)
            if iface.kind is not InterfaceKind.SERVICE:
                continue
            current = self._services.get(name)
            if iface.cardinality == "single" and current:
                conflict = {"interface": name, "current": current[0][0], "new": manifest.name}
                return (
                    f"Injection Conflict: Interface={name}, Current={current[0][0]}, New={manifest.name}",
                    conflict,
                )
        return None, None

    def register_failed(
        self, manifest: PluginManifest, status: str, reason: str,
        conflict: dict[str, str] | None = None,
    ) -> PluginRecord:
        """登记一个未注入的失败插件档案（unavailable/rejected），不进入待提交队列。"""
        record = PluginRecord(manifest=manifest, status=status, reason=reason, conflict=conflict)
        self._records.append(record)
        return record

    def register_remote(self, manifest: PluginManifest, plugin_dir: Any) -> PluginRecord:
        """进程外插件登记：spawn + 握手 + 能力上报 → 与进程内同构的声明 → 统一 register。

        命令缺失/启动失败/握手超时 → Unavailable（"插件自身运行环境不可用"）。
        """
        from focus.plugins.remote import RemotePlugin, build_remote_declaration

        try:
            remote = RemotePlugin(
                manifest.runtime.command, list(manifest.runtime.args), plugin_dir,
            )
            declaration = build_remote_declaration(remote, manifest)
        except Exception as exc:
            record = self.register_failed(
                manifest, "unavailable", f"插件自身运行环境不可用: {exc}",
            )
            logger.warning("插件 %s 远程加载失败: %s", manifest.name, exc)
            return record
        return self.register(manifest, declaration)

    def resolve_dependencies(self) -> None:
        # 两阶段解析：先对"可用服务集合"做不动点计算（依赖可指向任意加载序的插件，
        # 支持 A→B→C 依赖链），再按登记顺序统一提交，保证实现顺序稳定
        available = {name for name, entries in self._services.items() if entries}
        contributing: set[str] = set()
        changed = True
        while changed:
            changed = False
            for record, declaration in self._pending:
                if record.manifest.name in contributing:
                    continue
                if not all(req in available for req in record.manifest.requires):
                    continue
                contributing.add(record.manifest.name)
                declared = set(record.manifest.provides)
                for svc in declaration.services:
                    if svc in declared and svc not in available:
                        available.add(svc)
                        changed = True
        for record, declaration in self._pending:
            if record.manifest.name not in contributing:
                missing = [
                    name for name in record.manifest.requires
                    if name not in available
                ]
                record.status = "unavailable"
                record.missing = missing
                record.reason = "Missing dependency: " + ", ".join(missing)
                continue
            self._commit(record, declaration)
        self._pending.clear()

    def _commit(self, record: PluginRecord, declaration: PluginDeclaration) -> None:
        record.status = "active"
        record.injected = list(record.manifest.provides)
        declared = set(record.manifest.provides)
        self._tools.extend((record.manifest.name, tool_) for tool_ in declaration.tools)
        for name, hooks in declaration.hooks.items():
            if name in declared and hooks:
                self._hooks.setdefault(name, []).extend(
                    HookImpl(plugin=record.manifest.name, fn=hook) for hook in hooks
                )
        for name, impl in declaration.services.items():
            if name in declared:
                self._services.setdefault(name, []).append((record.manifest.name, impl))

    # === 查询面 ===

    def tools(self) -> list[Any]:
        return [tool_ for _, tool_ in self._tools]

    def list_interfaces(self) -> dict[str, dict]:
        """按接口返回实现插件名列表（稳定顺序）与接口属性，供调试视图展示编号顺序。"""
        result: dict[str, dict] = {}

        def entry(name: str, plugins: list[str]) -> dict:
            iface = self.catalog.get(name)
            return {
                "plugins": plugins,
                "read_only": iface.mutability == "read-only" if iface is not None else False,
                "cardinality": iface.cardinality if iface is not None else "multi",
            }

        if self._tools:
            result["tool"] = entry(
                "tool", list(dict.fromkeys(plugin for plugin, _ in self._tools)),
            )
        for name, impls in self._hooks.items():
            result[name] = entry(name, [impl.plugin for impl in impls])
        for name, entries in self._services.items():
            result[name] = entry(name, [plugin for plugin, _ in entries])
        return result

    def hooks(self, name: str) -> list[HookImpl]:
        return list(self._hooks.get(name, []))

    def declared_providers(self, name: str) -> list[str]:
        """返回声明提供该接口的插件名(含 pending/active,供构建期能力判定)。"""
        return [
            record.manifest.name
            for record in self._records
            if name in record.manifest.provides and record.status in ("pending", "active")
        ]

    def service(self, name: str) -> Any:
        iface = self.catalog.get(name)
        if iface is None or iface.kind is not InterfaceKind.SERVICE:
            raise KeyError(f"服务接口不存在: {name!r}")
        entries = self._services.get(name, [])
        if iface.cardinality == "single":
            return entries[0][1] if entries else None
        return [impl for _, impl in entries]

    def trace(
        self, interface: str, plugin: str, status: str,
        duration_ms: float, error: str | None = None,
    ) -> None:
        self._traces.append({
            "interface": interface,
            "plugin": plugin,
            "status": status,
            "duration_ms": round(duration_ms, 2),
            "error": error,
            "at": _now_iso(),
        })

    def traces(self) -> list[dict]:
        return list(reversed(self._traces))

    def list_plugins(self) -> list[dict]:
        result: list[dict] = []
        for record in self._records:
            result.append({
                "name": record.manifest.name,
                "version": record.manifest.version,
                "status": record.status,
                "injected": record.injected,
                "requires": record.manifest.requires,
                "missing": record.missing,
                "conflict": record.conflict,
                "reason": record.reason,
                "desktop_assets": self._asset_files(record.manifest.name, record.status),
            })
        return result

    def _asset_files(self, name: str, status: str) -> list[str]:
        """返回插件前端资源目录内的 js/css 文件名清单(仅 Active 插件,供前端注入)。"""
        if status != "active":
            return []
        assets = self._assets.get(name, {})
        assets_dir = assets.get("assets_dir")
        if assets_dir is None:
            return []
        import os

        return sorted(
            entry.name
            for entry in os.scandir(assets_dir)
            if entry.is_file()
            and entry.name.endswith((".js", ".css"))
            and not entry.name.endswith(".test.cjs")
        )
