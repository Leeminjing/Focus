"""本文件对外提供前端迁移验收的隔离真实 HTTP/SSE 服务。

输入为独立测试库与本用例保存的精确 checkpoint JSON；输出为生产 Workspace Patrol、Live、查询与控制响应。
具体工作流为装配既有生产 services/router，不运行 Provider；checkpoint 文件来自测试实际 Bootstrap 的内存 checkpoint。
示例：python -m uvicorn backend.tests.frontend_migration_server:app --port 8767。
"""

from contextlib import asynccontextmanager
import json
import os
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import messages_from_dict
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop.live_routes import live_loop_router
from backend.app.desktop.agent_loop.query_routes import loop_query_router
from backend.app.desktop.agent_loop.routes import agent_loop_router
from backend.app.desktop.agent_loop.service import AgentLoopService
from backend.app.desktop.agent_loop.workspace_patrol import WorkspacePatrolService


class Checkpointer:
    async def aget_tuple(self, config):
        with open(os.environ["FOCUS_FRONTEND_CHECKPOINT"], encoding="utf-8") as source:
            checkpoints = json.load(source)
        checkpoint_id = config["configurable"]["checkpoint_id"]
        saved = checkpoints[checkpoint_id]
        return SimpleNamespace(config=config, checkpoint={"id": checkpoint_id, "channel_values": {"messages": messages_from_dict(saved)}}, metadata={})


@asynccontextmanager
async def lifespan(app):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    desktop = SimpleNamespace(session_factory=sessions, checkpointer=Checkpointer(), app_config=SimpleNamespace(resolve_default_model_name=lambda: "test"))
    app.state.desktop_service = desktop
    app.state.workspace_patrol = WorkspacePatrolService(sessions, desktop)
    app.state.agent_loop_service = AgentLoopService(sessions)
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["null"], allow_methods=["GET", "POST"], allow_headers=["*"])
app.include_router(agent_loop_router)
app.include_router(live_loop_router)
app.include_router(loop_query_router)
