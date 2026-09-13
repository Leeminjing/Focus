"""
本文件对外提供 load_plugins 插件目录加载函数，作为插件发现 → 构建 → 登记的加载入口。

对外提供:
    load_plugins(registry, root, resolve=True, seen_names=None, disabled=None) — 扫描插件目录、解析清单、构建声明、登记注册表

输入:
    registry: PluginRegistry — 目标注入注册表
    root: str | Path — 插件根目录（默认 "plugins"）
    disabled: set[str] | None — 用户停用的插件名；命中的插件只登记不装配

输出:
    None — 插件经 registry.register / register_disabled 登记，最后 registry.resolve_dependencies 解析依赖

具体工作流:
    (1) 目录按名字典序遍历（加载顺序 = 稳定顺序）
    (2) 解析 plugin.json → PluginManifest；清单非法 → 拒绝接入（Rejected，理由含解析错误）
    (3) 清单 enabled=false（发布方默认关闭）或命中用户停用集合 → 以 disabled 登记但不装配，
        使其仍可被调试视图看到并重新启用；两者语义不同故分开判断，用户无法翻转发布方默认值
    (4) 读取 plugins/<name>/config.json 原样传入 PluginContext（系统不解释其含义）
    (5) 以 importlib 按文件路径加载 entry 模块（独立模块名，不污染 sys.modules 命名空间），
        调用 build_plugin(context)；构建抛错 → Unavailable（插件自身环境/配置问题）
    (6) build_plugin 返回非法对象或缺少 build_plugin → Rejected
    (7) 全部登记完成后统一 resolve_dependencies（两阶段：依赖可指向任意加载序的插件）

示例:
    registry = PluginRegistry(builtin_catalog())
    load_plugins(registry, "plugins")
"""

import importlib.util
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from focus.plugins.interfaces import InterfaceCatalog
from focus.plugins.registry import PluginRegistry
from focus.plugins.schemas import (
    PluginContext,
    PluginDeclaration,
    PluginManifest,
)

logger = logging.getLogger(__name__)

DEFAULT_PLUGINS_DIR = "plugins"


def _module_name(name: str) -> str:
    return f"focus_plugin_{name}"


def _load_config(plugin_dir: Path) -> dict[str, Any]:
    config_file = plugin_dir / "config.json"
    if not config_file.is_file():
        return {}
    try:
        raw = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"配置读取失败: {exc}") from exc
    return raw if isinstance(raw, dict) else {}


def _build_entry(plugin_dir: Path, entry: str, manifest: PluginManifest, registry: Any = None) -> Any:
    entry_file = plugin_dir / entry
    if not entry_file.is_file():
        raise ValueError(f"入口文件不存在: {entry}")
    spec = importlib.util.spec_from_file_location(_module_name(manifest.name), entry_file)
    if spec is None or spec.loader is None:
        raise ValueError(f"入口文件无法加载: {entry}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    build_plugin = getattr(module, "build_plugin", None)
    if not callable(build_plugin):
        raise ValueError("入口缺少 build_plugin(context) 函数")
    declaration = build_plugin(
        PluginContext(config=_load_config(plugin_dir), plugin_dir=plugin_dir, registry=registry)
    )
    if not isinstance(declaration, PluginDeclaration):
        raise ValueError("build_plugin 必须返回 PluginDeclaration")
    return declaration


def _collect_assets(plugin_dir: Path, manifest: PluginManifest) -> dict[str, Any]:
    """校验并收集插件的桌面 API 路由与前端资源;缺失时抛 ValueError(理由含缺失文件)。

    工作流:
        (1) http_routes: 加载插件目录内的路由模块,校验其暴露 router 属性
        (2) desktop_assets: 校验插件目录内的资源子目录存在
        (3) 均未声明时返回空 dict(与修订前行为一致)
    """
    assets: dict[str, Any] = {}
    if manifest.http_routes:
        route_file = plugin_dir / manifest.http_routes
        if not route_file.is_file():
            raise ValueError(f"路由模块缺失: {manifest.http_routes}")
        spec = importlib.util.spec_from_file_location(
            f"{_module_name(manifest.name)}_routes", route_file
        )
        if spec is None or spec.loader is None:
            raise ValueError(f"路由模块无法加载: {manifest.http_routes}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        router = getattr(module, "router", None)
        if not isinstance(router, APIRouter):
            raise ValueError(
                f"路由模块 router 必须是 fastapi.APIRouter: {manifest.http_routes}"
            )
        assets["router"] = router
    if manifest.desktop_assets:
        assets_dir = plugin_dir / manifest.desktop_assets
        if not assets_dir.is_dir():
            raise ValueError(f"前端资源目录缺失: {manifest.desktop_assets}")
        assets["assets_dir"] = assets_dir
    return assets


def load_plugins(
    registry: PluginRegistry,
    root: str | Path = DEFAULT_PLUGINS_DIR,
    *,
    resolve: bool = True,
    seen_names: set[str] | None = None,
    disabled: set[str] | None = None,
) -> None:
    """扫描 root 下的插件目录并登记。

    `disabled` 是用户停用集合：命中的插件不参与装配（跳过资源收集与构建，不注入任何接口），
    但仍以 disabled 状态登记，使调试视图能看到并重新启用它们。清单里的 enabled=false 是
    开发者发布的默认值，同样只登记不装配，且用户无法翻转——两者语义不同，故分开判断。
    """
    base = Path(root)
    if not base.is_dir():
        return
    user_disabled = disabled or set()
    for plugin_dir in sorted(base.iterdir(), key=lambda path: path.name.casefold()):
        manifest_file = plugin_dir / "plugin.json"
        if not plugin_dir.is_dir() or not manifest_file.is_file():
            continue
        try:
            raw = json.loads(manifest_file.read_text(encoding="utf-8"))
            manifest = PluginManifest.model_validate(raw)
        except Exception as exc:
            registry.register_failed(
                PluginManifest(name=plugin_dir.name, version="unknown"),
                "rejected", f"插件清单非法: {exc}",
            )
            logger.warning("插件清单非法: %s — %s", plugin_dir.name, exc)
            continue
        if manifest.name != plugin_dir.name:
            logger.warning("插件目录名与清单 name 不一致: %s != %s，已跳过", plugin_dir.name, manifest.name)
            continue
        if not manifest.enabled or manifest.name in user_disabled:
            # 同样占用跨根去重名额：更高优先级的根已声明该插件时，低优先级根不得重复登记。
            # can_toggle 区分停用来源——发布方关闭的不可由用户翻转。
            if seen_names is not None:
                seen_names.add(manifest.name)
            registry.register_disabled(manifest, can_toggle=manifest.name in user_disabled)
            continue
        if seen_names is not None and manifest.name in seen_names:
            registry.register_failed(
                manifest, "rejected", f"插件名称与更高优先级目录重复: {manifest.name}",
            )
            logger.warning("插件名称跨目录重复，已跳过: %s", manifest.name)
            continue
        if seen_names is not None:
            seen_names.add(manifest.name)
        # f18: 资源声明校验先于 register——缺失时插件整体 Unavailable,不进入待提交队列
        try:
            assets = _collect_assets(plugin_dir, manifest)
        except Exception as exc:
            registry.register_failed(
                manifest, "unavailable", f"插件自身资源不可用: {exc}",
            )
            logger.warning("插件 %s 资源收集失败: %s", manifest.name, exc)
            continue
        if assets:
            registry.register_assets(manifest.name, assets)
        if manifest.runtime is not None:
            registry.register_remote(manifest, plugin_dir)
            continue
        try:
            declaration = _build_entry(plugin_dir, manifest.entry, manifest, registry)
        except Exception as exc:
            registry.register_failed(
                manifest, "unavailable", f"插件自身运行环境不可用: {exc}",
            )
            logger.warning("插件 %s 构建失败: %s", manifest.name, exc)
            continue
        registry.register(manifest, declaration)
    if resolve:
        registry.resolve_dependencies()
