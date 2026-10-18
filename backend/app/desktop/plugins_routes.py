"""本文件对外提供 plugins_router，作为插件调试视图的 HTTP 查询接口层。

输入为带 X-Focus-Session 的桌面请求（由统一 AuthMiddleware 保护）；输出为插件状态
JSON 或最近执行轨迹 JSON。具体工作流为调用 get_plugin_registry 懒加载单例（与 agent
装配共享同一实例），list_plugins 返回插件卡片数据（name/version/status/injected/
requires/missing/conflict/reason）与 activation 生效范围，traces 返回环形缓冲轨迹（最新在前）。示例：
`app.include_router(plugins_router)`。
"""

from fastapi import APIRouter

from focus.plugins import get_plugin_registry, reload_plugins
from focus.plugins.registry import PluginRegistry

plugins_router = APIRouter()


def _registry_response(registry: PluginRegistry) -> dict:
    """附带生效策略；不将注册表中的资源声明视为端点已就绪的证据。"""
    return {
        "plugins": registry.list_plugins(),
        "interfaces": registry.list_interfaces(),
        "activation": {
            "registry": "immediate",
            "tools_and_hooks": "next_run",
            "running_runs": "unchanged",
            "http_routes": "process_restart",
            "desktop_assets": "process_restart",
        },
    }


@plugins_router.get("/desktop/api/plugins")
async def list_plugins() -> dict:
    """返回注册表状态与生效范围；active 不代表插件 UI/API 已挂载。"""
    registry = get_plugin_registry()
    return _registry_response(registry)


@plugins_router.get("/desktop/api/plugins/traces")
async def plugin_traces() -> dict:
    return {"traces": get_plugin_registry().traces()}


@plugins_router.post("/desktop/api/plugins/reload")
async def reload_global_plugins() -> dict:
    """重建内置 + 全局插件注册表，工具与 hooks 在下次 Run 装配时生效。

    正在运行的 Run 保留原快照；插件 HTTP 路由与静态资源挂载的增删改需要
    重启后端进程（桌面使用时退出并重新启动 Focus），此接口不重新挂载资源。
    """
    registry = reload_plugins()
    return _registry_response(registry)
