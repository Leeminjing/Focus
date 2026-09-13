"""本文件对外提供 plugins_router，作为插件调试视图的 HTTP 查询与启停接口层。

输入为带 X-Focus-Session 的桌面请求（由统一 AuthMiddleware 保护）；输出为插件状态
JSON、最近执行轨迹 JSON 或启停后的最新插件状态。具体工作流为调用 get_plugin_registry
懒加载单例（与 agent 装配共享同一实例），list_plugins 返回插件卡片数据
（name/version/status/injected/requires/missing/conflict/reason/can_toggle），
set_plugin_enabled 写入用户停用偏好并重建注册表，traces 返回环形缓冲轨迹（最新在前）。
示例：`app.include_router(plugins_router)`。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from focus.plugins import get_plugin_registry, reload_plugins
from focus.plugins.preferences import disabled_plugins, preference_path, set_plugin_disabled

plugins_router = APIRouter()


class PluginToggle(BaseModel):
    enabled: bool


@plugins_router.get("/desktop/api/plugins")
async def list_plugins() -> dict:
    registry = get_plugin_registry()
    return {"plugins": registry.list_plugins(), "interfaces": registry.list_interfaces()}


@plugins_router.get("/desktop/api/plugins/traces")
async def plugin_traces() -> dict:
    return {"traces": get_plugin_registry().traces()}


@plugins_router.post("/desktop/api/plugins/reload")
async def reload_global_plugins() -> dict:
    """会话内重新加载内置 + ~/.focus/plugins 组合根，返回插件状态与接口映射。"""
    registry = reload_plugins()
    return {"plugins": registry.list_plugins(), "interfaces": registry.list_interfaces()}


@plugins_router.put("/desktop/api/plugins/{name}/enabled")
async def set_plugin_enabled(name: str, body: PluginToggle) -> dict:
    """写入用户启停偏好并重建注册表。

    已知插件才允许切换：未知名字一律 404，避免偏好文件被任意键写脏。正在运行的 run 使用
    装配时快照，不受影响；下一次装配读到新注册表。
    """
    known = {plugin["name"] for plugin in get_plugin_registry().list_plugins()}
    if name not in known:
        raise HTTPException(404, f"插件不存在: {name}")
    set_plugin_disabled(name, not body.enabled)
    registry = reload_plugins()
    return {
        "plugins": registry.list_plugins(),
        "interfaces": registry.list_interfaces(),
        "disabled": sorted(disabled_plugins()),
        "preference_path": str(preference_path()),
    }
