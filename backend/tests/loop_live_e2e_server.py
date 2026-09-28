r"""本文件对外提供首轮 Loop 桌面验收所需的真实只读 HTTP 服务。

输入为隔离测试库 URL、Loop/Context 身份和固定 revision checkpoint 消息；输出为生产 Live、Console、会话、关联查询与 SSE 路由。
具体工作流为单独进程连接同一测试库，复用生产路由与查询服务，向 Electron 提供正在运行的同一 Run 的持久状态；仅缺失的历史 checkpoint 由确定性消息提供。
示例：`python -m uvicorn backend.tests.loop_live_e2e_server:app --port 8766`。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.desktop.persistence_registry
from backend.app.desktop.agent_loop.live_routes import live_loop_router
from backend.app.desktop.agent_loop.query_routes import loop_query_router
from backend.app.desktop.agent_loop.service import AgentLoopService


class RevisionCheckpointer:
    async def aget_tuple(self, config):
        messages = [
            HumanMessage(content=item["content"], id=item["id"])
            if item["role"] == "human" else AIMessage(content=item["content"], id=item["id"])
            for item in json.loads(os.environ["FOCUS_LOOP_E2E_HISTORY"])
        ]
        return SimpleNamespace(
            config=config,
            checkpoint={"channel_values": {"messages": messages}},
            metadata={},
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app.state.desktop_service = SimpleNamespace(session_factory=sessions, checkpointer=RevisionCheckpointer())
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
app.include_router(live_loop_router)
app.include_router(loop_query_router)


@app.get("/desktop/api/agent-loops/by-context/{context_id}")
async def by_context(context_id: str):
    if context_id != os.environ["FOCUS_LOOP_E2E_CONTEXT_ID"]:
        return None
    return await AgentLoopService(app.state.desktop_service.session_factory).get(os.environ["FOCUS_LOOP_E2E_LOOP_ID"])


@app.get("/desktop/api/agent-loops/{loop_id}")
async def loop_snapshot(loop_id: str):
    if loop_id != os.environ["FOCUS_LOOP_E2E_LOOP_ID"]:
        raise HTTPException(404, "Loop 不存在")
    return await AgentLoopService(app.state.desktop_service.session_factory).get(loop_id)
