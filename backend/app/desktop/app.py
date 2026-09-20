"""
本文件对外提供 mount_desktop，把桌面能力内嵌到唯一 FastAPI Gateway。

输入为已经创建的 Gateway `FastAPI` 实例；输出为挂载完成的 `/desktop/api` 普通/Live 路由、插件资产和
`/desktop/` 静态页面。具体工作流为先注册 Context、Loop、压缩、Memory、模型设置与插件 API，
再挂载插件前端和 Desktop 静态目录，从而保持页面与 API 同源且不创建第二个后端。
DesktopService 由 Gateway lifespan 构造并复用同一数据库、checkpointer、store 和 StreamBridge。
示例：`mount_desktop(app)`；开发时运行
`python -m uvicorn backend.app.gateway.app:app --host 127.0.0.1 --port 8765`。
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
DESKTOP_DIR = ROOT / "desktop"


def mount_desktop(app) -> None:
    """将桌面路由与静态资源挂载到 Gateway 应用。

    工作流:
        (1) include_router(desktop_router) — 提供 /desktop/api 全部接口
        (2) include_router(compression_router) — 提供压缩摘要与消息快照接口
        (3) include_router(plugins_router) — 提供插件调试视图查询接口
        (4) include_router(model_settings_router) — 提供模型设置的读取/保存/测试/引用查询接口
        (5) mount_plugin_assets(app) — 挂载 Active 插件的桌面 API 路由与前端静态资源
        (6) app.mount("/desktop", StaticFiles(...)) — 提供 index.html / app.js / styles.css

    路由注册先于静态挂载，确保 /desktop/api/* 优先由 API 路由处理，静态挂载兜底。
    """
    from fastapi.staticfiles import StaticFiles

    from backend.app.desktop.compression_routes import compression_router
    from backend.app.desktop.memory_routes import memory_router
    from backend.app.desktop.model_settings_routes import model_settings_router
    from backend.app.desktop.plugins_routes import plugins_router
    from backend.app.desktop.routes import desktop_router
    from backend.app.desktop.agent_loop.routes import agent_loop_router
    from backend.app.desktop.agent_loop.query_routes import loop_query_router
    from backend.app.desktop.agent_loop.live_routes import live_loop_router

    app.include_router(desktop_router)
    app.include_router(agent_loop_router)
    app.include_router(loop_query_router)
    app.include_router(live_loop_router)
    app.include_router(compression_router)
    app.include_router(plugins_router)
    app.include_router(memory_router)
    app.include_router(model_settings_router)
    mount_plugin_assets(app)
    app.mount("/desktop", StaticFiles(directory=str(DESKTOP_DIR), html=True), name="desktop")
    logger.info("桌面路由与静态资源已挂载 (/desktop/api, /desktop)")


def mount_plugin_assets(app) -> None:
    """挂载 Active 插件的桌面 API 路由与前端静态资源(f18 通用挂载约定)。

    工作流:
        (1) get_plugin_registry() 触发插件懒加载(与调试视图共享同一实例)
        (2) 每个 Active 插件的路由以 /desktop/api/plugin/{name} 为强制前缀注册
        (3) 其前端资源目录挂载为 /plugins/{name}/desktop 静态路径

    插件被禁用或卸载后,本函数不再注册其资源(下次进程启动生效)。
    """
    from focus.plugins import get_plugin_registry

    registry = get_plugin_registry()
    for name, assets in registry.active_assets().items():
        if (router := assets.get("router")) is not None:
            app.include_router(router, prefix=f"/desktop/api/plugin/{name}")
        if (assets_dir := assets.get("assets_dir")) is not None:
            from fastapi.staticfiles import StaticFiles

            app.mount(
                f"/plugins/{name}/desktop",
                StaticFiles(directory=str(assets_dir)),
                name=f"plugin-{name}-desktop",
            )
    if registry.active_assets():
        logger.info("插件资源已挂载: %s", ", ".join(sorted(registry.active_assets())))
