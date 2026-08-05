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
        (2) app.mount("/desktop", StaticFiles(...)) — 提供 index.html / app.js / styles.css

    路由注册先于静态挂载，确保 /desktop/api/* 优先由 API 路由处理，静态挂载兜底。
    """
    from fastapi.staticfiles import StaticFiles

    from backend.app.desktop.routes import desktop_router

    app.include_router(desktop_router)
    app.mount("/desktop", StaticFiles(directory=str(DESKTOP_DIR), html=True), name="desktop")
    logger.info("桌面路由与静态资源已挂载 (/desktop/api, /desktop)")
