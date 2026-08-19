"""本文件对外提供 plugins_router，作为插件调试视图的 HTTP 查询接口层。

输入为带 X-Focus-Session 的桌面请求（由统一 AuthMiddleware 保护）；输出为插件状态
JSON 或最近执行轨迹 JSON。具体工作流为调用 get_plugin_registry 懒加载单例（与 agent
装配共享同一实例），list_plugins 返回插件卡片数据（name/version/status/injected/
requires/missing/conflict/reason），traces 返回环形缓冲轨迹（最新在前）。示例：
`app.include_router(plugins_router)`。
"""

from fastapi import APIRouter

from focus.plugins import get_plugin_registry

plugins_router = APIRouter()


@plugins_router.get("/desktop/api/plugins")
async def list_plugins() -> dict:
    registry = get_plugin_registry()
    return {"plugins": registry.list_plugins(), "interfaces": registry.list_interfaces()}


@plugins_router.get("/desktop/api/plugins/traces")
async def plugin_traces() -> dict:
    return {"traces": get_plugin_registry().traces()}
