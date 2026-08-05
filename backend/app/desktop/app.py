"""
本文件对外提供桌面 FastAPI 应用 `app`，作为浏览器开发模式与 Electron API 的独立入口。

输入为 `FOCUS_DATABASE_URL`、`FOCUS_DESKTOP_SESSION` 等本地环境变量；输出为只绑定方
指定 loopback 地址使用的 FastAPI 应用。具体工作流为初始化 PostgreSQL engine、
AsyncPostgresSaver、AsyncPostgresStore、StreamBridge 和 DesktopService，随后提供同端口
静态页面与 `/desktop/api`。示例：`uvicorn backend.app.desktop.app:app --port 8765`。
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import asyncio
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
HARNESS_ROOT = ROOT / "backend" / "packages" / "harness"
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.desktop.models import DesktopWorkspace  # noqa: F401
from backend.app.desktop.routes import desktop_router
from backend.app.desktop.service import DesktopService
from focus.config import reload_app_config
from focus.config.checkpointer_config import CheckpointerConfig
from focus.config.database_config import DatabaseConfig
from focus.config.langgraph_store_config import LanggraphStoreConfig
from focus.runtime.stream_bridge.memory import MemoryStreamBridge


if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


DESKTOP_DIR = ROOT / "desktop"


def _database_url() -> str:
    return os.getenv(
        "FOCUS_DATABASE_URL",
        "postgresql+asyncpg://focus:qweasdzxc123@127.0.0.1:7221/focus",
    )


def _postgres_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    os.environ.setdefault("OPENAI_API_KEY", "desktop-not-configured")
    os.environ.setdefault("JWT_SECRET", "desktop-unused")
    config = reload_app_config(str(ROOT / "config.yaml"))
    database = DatabaseConfig.model_validate({**config.database.model_dump(), "url": _database_url()})
    config = config.model_copy(update={
        "database": database,
        "checkpointer": CheckpointerConfig(type="postgres"),
        "langgraph_store": LanggraphStoreConfig(
            backend="async_postgres", vector_enabled=False,
            embed=config.langgraph_store.embed, dims=config.langgraph_store.dims,
            fields=config.langgraph_store.fields,
        ),
    })
    engine = create_async_engine(database.url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres import AsyncPostgresStore

    async with AsyncPostgresSaver.from_conn_string(_postgres_dsn(database.url)) as checkpointer:
        async with AsyncPostgresStore.from_conn_string(_postgres_dsn(database.url)) as store:
            bridge = MemoryStreamBridge(queue_maxsize=1024)
            service = DesktopService(sessions, checkpointer, store, bridge, config)
            app.state.session_key = os.getenv("FOCUS_DESKTOP_SESSION", "focus-dev-session")
            app.state.desktop_service = service
            await service.start()
            try:
                yield
            finally:
                await service.close()
    await engine.dispose()


app = FastAPI(title="Focus Desktop PoC", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(null|https?://(127\.0\.0\.1|localhost)(:\d+)?)$",
    allow_methods=["*"],
    allow_headers=["Content-Type", "X-Focus-Session", "Last-Event-ID"],
)
app.include_router(desktop_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(DESKTOP_DIR / "index.html")


@app.get("/app.js")
async def javascript() -> FileResponse:
    return FileResponse(DESKTOP_DIR / "app.js", media_type="text/javascript")


@app.get("/styles.css")
async def stylesheet() -> FileResponse:
    return FileResponse(DESKTOP_DIR / "styles.css", media_type="text/css")


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.app.desktop.app:app",
        host="127.0.0.1",
        port=int(os.getenv("FOCUS_DESKTOP_PORT", "8765")),
        loop="backend.app.desktop.app:selector_loop_factory",
    )
