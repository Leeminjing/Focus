"""
本文件是桌面功能的内嵌挂载入口，不再创建第二个 FastAPI app。

Gateway（`backend.app.gateway.app`）作为唯一 FastAPI 应用（设计决策 1）：
  - 挂载 `/desktop/api` 路由（desktop_router，含 loopback 会话密钥与对等地址校验）
  - 挂载 `/desktop/` 静态资源（desktop/index.html、app.js、styles.css）

页面与 API 同源，无需 CORS。DesktopService 在 gateway lifespan 内构造并挂到
app.state（复用 langgraph_runtime 的 checkpointer / store / stream_bridge 与全局
数据库 engine）。

开发模式：`python -m uvicorn backend.app.gateway.app:app --host 127.0.0.1 --port 8765`
（与 Electron 生产模式同为 Gateway 应用）
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
        (4) mount_plugin_assets(app) — 挂载 Active 插件的桌面 API 路由与前端静态资源
        (5) app.mount("/desktop", StaticFiles(...)) — 提供 index.html / app.js / styles.css

    路由注册先于静态挂载，确保 /desktop/api/* 优先由 API 路由处理，静态挂载兜底。
    """
    from fastapi.staticfiles import StaticFiles

    from backend.app.desktop.compression_routes import compression_router
    from backend.app.desktop.plugins_routes import plugins_router
    from backend.app.desktop.routes import desktop_router

    app.include_router(desktop_router)
    app.include_router(compression_router)
    app.include_router(plugins_router)
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
