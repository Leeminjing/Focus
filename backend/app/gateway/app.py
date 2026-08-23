"""
本文件对外提供 FastAPI 应用实例 app，作为 backend 网关的统一入口。

对外提供:
    app: FastAPI — 已绑定 lifespan 的 FastAPI 应用实例

具体工作流:
    (1) 定义 lifespan 异步上下文管理器
    (2) lifespan 内部加载 AppConfig（组合根），传入 langgraph_runtime 进行依赖注入
    (3) async with langgraph_runtime(app, app_config) 管理核心资源生命周期
    (4) 在同一 lifespan 内构造 DesktopService 并挂载到 app.state（决策 1、8、11）
    (5) 创建 FastAPI 实例并传入 lifespan
    (6) 注册统一会话保护中间件与路由，并挂载桌面路由与 /desktop/ 静态资源（决策 1）
    (7) 模块级导出 app 实例，供 uvicorn 等 ASGI server 直接引用

示例:
    uvicorn backend.app.gateway.app:app --host 0.0.0.0 --port 8000
    uvicorn backend.app.gateway.app:app --host 127.0.0.1 --port 8765   # Electron 桌面模式
"""

import asyncio
import os

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from backend.app.gateway.deps import langgraph_runtime

# 导入 desktop models 以注册到 Base.metadata（供 Alembic autogenerate 发现）
import backend.app.desktop.models  # noqa: F401

# 在所有配置加载之前注入 .env 环境变量
load_dotenv()
# 全局态 ~/.focus/.env 密钥：仅补缺，不覆盖已设置环境变量（仓库态/真实 env 优先）
from focus.config.layered import load_global_dotenv

load_global_dotenv()

# asyncpg 在 Windows 上需要 Selector 事件循环（桌面链路依赖同一策略）
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    from focus.config import get_app_config

    app_config = get_app_config("config.yaml")
    # FOCUS_DATABASE_URL 环境变量覆写数据库 URL（Electron 启动链传入，与独立桌面入口行为一致）
    database_url = os.getenv("FOCUS_DATABASE_URL")
    if database_url and app_config.database is not None:
        from focus.config.database_config import DatabaseConfig

        app_config = app_config.model_copy(update={
            "database": DatabaseConfig.model_validate({
                **app_config.database.model_dump(), "url": database_url,
            }),
        })
    logger.info("AppConfig 已加载，开始初始化 agent 核心资源...")

    async with langgraph_runtime(app, app_config):
        # 桌面功能内嵌：构造 DesktopService，复用 langgraph_runtime 挂载的
        # checkpointer / store / stream_bridge 与全局数据库 engine（决策 1、8、11）
        from backend.app.desktop.service import DesktopService
        from focus.persistence.engine import get_session_factory

        sessions = get_session_factory()
        service = DesktopService(
            session_factory=sessions,
            checkpointer=app.state.checkpointer,
            store=app.state.store,
            bridge=app.state.stream_bridge,
            app_config=app_config,
            run_manager=app.state.run_manager,
        )
        app.state.desktop_service = service
        app.state.session_key = os.getenv("FOCUS_DESKTOP_SESSION", "focus-dev-session")
        await service.start()
        try:
            yield
        finally:
            await service.close()

    logger.info("FastAPI lifespan 关闭，agent 核心资源已释放")


app = FastAPI(lifespan=lifespan)

# 注册中间件（统一桌面会话保护：loopback + X-Focus-Session）
from backend.app.gateway.middleware.auth import AuthMiddleware

app.add_middleware(AuthMiddleware)
logger.info("AuthMiddleware（统一会话保护）已注册")

# 注册路由（统一运行接口 thread_runs；uploads 随网页端移除）
from backend.app.gateway.routers.thread_runs import router as thread_runs_router

app.include_router(thread_runs_router, prefix="/api/threads")
logger.info("路由已注册: thread_runs")

# 桌面功能内嵌（决策 1）：/desktop/api 路由 + /desktop/ 静态资源，页面与 API 同源
from backend.app.desktop.app import mount_desktop

mount_desktop(app)
logger.info("桌面路由与静态资源已挂载 (/desktop/api, /desktop)")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Windows 下供 uvicorn 使用的 Selector 事件循环工厂。

    psycopg async 模式不能运行在 Windows 默认的 ProactorEventLoop 上；
    uvicorn 在导入应用模块之前就创建事件循环，模块级 set_event_loop_policy
    对 uvicorn 无效，必须通过 `--loop backend.app.gateway.app:selector_loop_factory`
    传入（uvicorn 0.36+ 将自定义 loop 字符串作为 loop factory 直接使用）。
    """
    return asyncio.SelectorEventLoop()
