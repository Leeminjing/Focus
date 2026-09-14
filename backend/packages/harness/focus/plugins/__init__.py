"""
本文件对外提供 get_plugin_registry 懒加载单例函数，作为插件注册表的进程级访问入口。

对外提供:
    get_plugin_registry(root=None) — 默认合并仓库内置插件与 ~/.focus/plugins；显式 root 时只加载该目录

输入:
    root: str | Path | None — 显式插件根；None 表示仓库内置根 + 全局用户根

输出:
    PluginRegistry — 已完成加载与依赖解析的注册表（同一 root 多次调用返回同一实例）

具体工作流:
    (1) 首次调用时构造 builtin_catalog() 目录与 PluginRegistry
    (2) 默认依次扫描仓库内置 root 与全局用户 root，再统一解析跨根依赖
    (3) 实例按 root 缓存于模块级字典；与 get_app_config / get_mcp_tools_cached 同模式，
        不经 FastAPI lifespan，装配与调试接口共享同一实例

示例:
    registry = get_plugin_registry()
    tools = registry.tools()
"""

from pathlib import Path

from focus.plugins.interfaces import builtin_catalog
from focus.plugins.registry import PluginRegistry

_registries: dict[str, PluginRegistry] = {}
_DEFAULT_KEY = "<builtin+global>"
_BUILTIN_PLUGINS_DIR = Path(__file__).resolve().parents[5] / "plugins"


def _plugin_roots(root: str | Path | None) -> tuple[str, list[Path]]:
    if root is not None:
        return str(root), [Path(root)]

    from focus.config.layered import global_home

    return _DEFAULT_KEY, [_BUILTIN_PLUGINS_DIR, global_home() / "plugins"]


def _build_registry(roots: list[Path]) -> PluginRegistry:
    registry = PluginRegistry(builtin_catalog())
    from focus.plugins.loader import load_plugins

    seen_names: set[str] = set()
    for plugin_root in roots:
        load_plugins(registry, plugin_root, resolve=False, seen_names=seen_names)
    registry.resolve_dependencies()
    return registry


def get_plugin_registry(root: str | Path | None = None) -> PluginRegistry:
    key, roots = _plugin_roots(root)
    if key not in _registries:
        _registries[key] = _build_registry(roots)
    return _registries[key]


def reload_plugins(root: str | Path | None = None) -> PluginRegistry:
    """重建默认组合根或指定插件根的注册表并替换缓存（会话内加载新插件）。

    装配模式写入新插件后调用；新实例重新扫描、重新解析依赖，避免已登记插件重复。
    正在运行中的 run 使用装配时快照，不受影响；下一次 run 装配时读到新注册表。
    """
    key, roots = _plugin_roots(root)
    _registries[key] = _build_registry(roots)
    return _registries[key]
