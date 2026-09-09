"""MCP 常驻 session 的异步任务生命周期回归测试。"""

import asyncio
from contextlib import asynccontextmanager
import sys
import types

import anyio
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


class _PassThroughMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        return await call_next(request)


def test_persistent_mcp_session_does_not_leak_request_cancel_scope(monkeypatch):
    """请求加载 MCP 后应正常结束，session 由同一后台任务进入和退出。"""
    import focus.mcp.tools as mcp_tools

    entered_by: list[asyncio.Task] = []
    exited_by: list[asyncio.Task] = []

    class FakeClient:
        def __init__(self, config):
            self.config = config

        @asynccontextmanager
        async def session(self, server_name):
            entered_by.append(asyncio.current_task())
            async with anyio.create_task_group():
                yield object()
            exited_by.append(asyncio.current_task())

    async def fake_load_tools(session):
        return []

    client_module = types.ModuleType("langchain_mcp_adapters.client")
    client_module.MultiServerMCPClient = FakeClient
    tools_module = types.ModuleType("langchain_mcp_adapters.tools")
    tools_module.load_mcp_tools = fake_load_tools
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", client_module)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.tools", tools_module)

    async def endpoint(request):
        tools = await mcp_tools.load_mcp_tools({"playwright": {}})
        return PlainTextResponse(str(len(tools)))

    app = Starlette(routes=[Route("/", endpoint)])
    app.add_middleware(_PassThroughMiddleware)

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.text == "0"
        client.portal.call(mcp_tools.close_mcp_sessions)

    assert len(entered_by) == 1
    assert exited_by == entered_by
