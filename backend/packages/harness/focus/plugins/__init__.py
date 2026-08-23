"""
本文件对外提供 get_plugin_registry 懒加载单例函数，作为插件注册表的进程级访问入口。

对外提供:
    get_plugin_registry(root=DEFAULT_PLUGINS_DIR) — 返回指定插件根目录的注册表单例

输入:
    root: str | Path — 插件根目录，不同 root 各自缓存；默认 "plugins"

输出:
    PluginRegistry — 已完成加载与依赖解析的注册表（同一 root 多次调用返回同一实例）

具体工作流:
    (1) 首次调用时构造 builtin_catalog() 目录与 PluginRegistry
    (2) load_plugins 扫描 root 并登记全部插件、解析依赖
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


def get_plugin_registry(root: str | Path = "plugins") -> PluginRegistry:
    key = str(root)
    if key not in _registries:
        registry = PluginRegistry(builtin_catalog())
        from focus.plugins.loader import load_plugins

        load_plugins(registry, root)
        _registries[key] = registry
    return _registries[key]


def reload_plugins(root: str | Path = "plugins") -> PluginRegistry:
    """重建指定插件根的注册表并替换缓存（会话内加载新插件）。

    装配模式写入新插件后调用；新实例重新扫描、重新解析依赖，避免已登记插件重复。
    正在运行中的 run 使用装配时快照，不受影响；下一次 run 装配时读到新注册表。
    """
    key = str(root)
    registry = PluginRegistry(builtin_catalog())
    from focus.plugins.loader import load_plugins

    load_plugins(registry, root)
    _registries[key] = registry
    return registry
