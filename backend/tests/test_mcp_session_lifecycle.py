"""MCP 常驻 session 的异步任务生命周期回归测试。"""

import asyncio
from contextlib import asynccontextmanager
import sys
import types

import anyio
import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


class _PassThroughMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        return await call_next(request)


async def _until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


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


@pytest.fixture
def fake_mcp(monkeypatch):
    import focus.mcp.tools as tools
    import focus.mcp.cache as cache

    entered = []
    exited = []
    gates = {}

    class Client:
        def __init__(self, config):
            pass

        @asynccontextmanager
        async def session(self, name):
            entered.append(name)
            try:
                if name in gates:
                    await gates[name].wait()
                yield name
            finally:
                exited.append(name)

    async def load(name):
        return []

    clients = types.ModuleType("langchain_mcp_adapters.client")
    clients.MultiServerMCPClient = Client
    adapters = types.ModuleType("langchain_mcp_adapters.tools")
    adapters.load_mcp_tools = load
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", clients)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.tools", adapters)
    for name, value in (("_MCP_CLIENTS", []), ("_MCP_SESSIONS", []), ("_MCP_CONNECTIONS", {})):
        monkeypatch.setattr(tools, name, value)
    monkeypatch.setattr(tools, "_MCP_LIMITERS", {})
    for name in ("_cache", "_loads", "_expiry"):
        monkeypatch.setattr(cache, name, {})
    return tools, entered, exited, gates


def test_slow_server_does_not_serialize_other_connections(fake_mcp):
    tools, entered, _, gates = fake_mcp

    async def scenario():
        gates["slow"] = asyncio.Event()
        loading = asyncio.create_task(tools.load_mcp_tools({"slow": {}, "fast": {}}))
        try:
            for _ in range(20):
                await asyncio.sleep(0.001)
                if "fast" in entered:
                    break
            assert "fast" in entered
        finally:
            gates["slow"].set()
            await loading
            await tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_initialization_deadline_includes_queue_and_bounds_all_servers(fake_mcp, monkeypatch):
    tools, entered, exited, gates = fake_mcp
    monkeypatch.setattr(tools, "MCP_INITIALIZATION_TIMEOUT", 0.05)
    monkeypatch.setattr(tools, "MCP_MAX_CONCURRENT_INITIALIZATIONS", 2)

    async def scenario():
        names = [f"slow-{i}" for i in range(20)]
        gates.update({name: asyncio.Event() for name in names})
        try:
            result = await asyncio.wait_for(tools.load_mcp_tools(dict.fromkeys(names, {})), 0.3)
            assert result.failed_servers == names
            assert all("截止时间" in failure.reason for failure in result.failures)
            assert len(entered) == 2
            # owner 任务处理取消和资源清理后，排队服务也不会稍后重新启动。
            await _until(lambda: not tools._MCP_SESSIONS)
            assert exited == entered
            assert not tools._MCP_SESSIONS
            assert not tools._MCP_CONNECTIONS
        finally:
            await tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_tool_discovery_shares_handshake_deadline_and_can_retry(fake_mcp, monkeypatch):
    tools, entered, _, _ = fake_mcp
    from langchain_core.tools import StructuredTool

    monkeypatch.setattr(tools, "MCP_INITIALIZATION_TIMEOUT", 0.05)

    async def scenario():
        discovery = asyncio.Event()

        async def load(name):
            if name == "slow":
                await discovery.wait()
            return [StructuredTool.from_function(lambda: name, name=name, description="test")]

        monkeypatch.setattr(sys.modules["langchain_mcp_adapters.tools"], "load_mcp_tools", load)
        try:
            result = await tools.load_mcp_tools({"slow": {}, "fast": {}})
            assert [tool.name for tool in result] == ["fast"]
            assert result.failed_servers == ["slow"]
            discovery.set()
            recovered = await tools.load_mcp_tools({"slow": {}, "fast": {}})
            assert [tool.name for tool in recovered] == ["slow", "fast"]
            assert recovered.healthy
            assert entered.count("fast") == 1
            assert entered.count("slow") == 2
        finally:
            await tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_deadline_does_not_wait_for_slow_transport_cleanup(fake_mcp, monkeypatch):
    tools, _, _, _ = fake_mcp
    monkeypatch.setattr(tools, "MCP_INITIALIZATION_TIMEOUT", 0.04)

    async def scenario():
        cleanup_started = asyncio.Event()
        finish_cleanup = asyncio.Event()

        class Client:
            def __init__(self, config):
                pass

            @asynccontextmanager
            async def session(self, name):
                try:
                    await asyncio.Event().wait()
                    yield name
                finally:
                    cleanup_started.set()
                    await finish_cleanup.wait()

        monkeypatch.setattr(sys.modules["langchain_mcp_adapters.client"], "MultiServerMCPClient", Client)
        try:
            result = await asyncio.wait_for(tools.load_mcp_tools({"slow": {}}), 0.3)
            assert result.failed_servers == ["slow"]
            await asyncio.wait_for(cleanup_started.wait(), 0.3)
            assert tools._MCP_SESSIONS  # 清理尚未结束，装配已经按时返回。
        finally:
            finish_cleanup.set()
            await tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_concurrency_limit_is_shared_across_configurations(fake_mcp, monkeypatch):
    tools, entered, _, gates = fake_mcp
    monkeypatch.setattr(tools, "MCP_MAX_CONCURRENT_INITIALIZATIONS", 2)

    async def scenario():
        gates.update({name: asyncio.Event() for name in ("a", "b", "c", "d")})
        first = asyncio.create_task(tools.load_mcp_tools({"a": {}, "b": {}}))
        second = asyncio.create_task(tools.load_mcp_tools({"c": {}, "d": {}}))
        try:
            await _until(lambda: len(entered) == 2)
            assert entered == ["a", "b"]
            gates["a"].set()
            gates["b"].set()
            await first
            await _until(lambda: len(entered) == 4)
            assert entered == ["a", "b", "c", "d"]
        finally:
            for gate in gates.values():
                gate.set()
            await asyncio.gather(first, second)
            await tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_idle_reclamation_waits_for_last_run_and_evicts_snapshot(fake_mcp, monkeypatch):
    tools, entered, exited, _ = fake_mcp
    import focus.mcp.cache as cache

    monkeypatch.setattr(tools, "MCP_SESSION_IDLE_TIMEOUT", 0.02)

    async def scenario():
        first_run = tools.begin_mcp_run()
        try:
            first = await tools.load_mcp_tools({"shared": {"version": 1}})
            # 第二个 Run 复用旧连接，同时配置发生变化建立新连接。
            second_run = tools.begin_mcp_run()
            try:
                second = await tools.load_mcp_tools({"shared": {"version": 1}})
                await tools.load_mcp_tools({"shared": {"version": 2}})
                cache._cache[(asyncio.get_running_loop(), "old")] = first
                cache._cache[(asyncio.get_running_loop(), "new")] = second
                await asyncio.sleep(0.04)
                assert not exited
            finally:
                tools.end_mcp_run(second_run)
            await asyncio.sleep(0.04)
            assert len(exited) == 1  # 新配置已无 Run 引用。
            assert first.healthy
        finally:
            tools.end_mcp_run(first_run)
        await asyncio.sleep(0.04)
        assert len(entered) == len(exited) == 2
        assert not tools._MCP_SESSIONS
        assert not tools._MCP_CLIENTS
        assert not tools._MCP_CONNECTIONS
        assert not cache._cache
        await tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_cached_partial_result_is_shared_by_concurrent_waiters(fake_mcp, monkeypatch):
    tools, entered, _, gates = fake_mcp
    import focus.mcp.cache as cache
    from focus.config.extensions_config import ExtensionsConfig

    config = ExtensionsConfig.model_validate({"mcpServers": {
        "slow": {"enabled": True, "type": "http", "url": "https://test.invalid/mcp", "description": "test"},
    }})
    monkeypatch.setattr(cache, "get_extensions_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(tools, "MCP_INITIALIZATION_TIMEOUT", 0.04)

    async def scenario():
        gates["slow"] = asyncio.Event()
        try:
            results = await asyncio.wait_for(asyncio.gather(*[
                cache.get_mcp_tools_cached(force_refresh=True) for _ in range(8)
            ]), 0.2)
            assert all(result is results[0] for result in results)
            assert results[0].failed_servers == ["slow"]
            assert entered == ["slow"]  # 失败时也不会逐个串行重试。
            gates["slow"].set()
            assert (await cache.get_mcp_tools_cached()).healthy
            assert entered == ["slow", "slow"]
        finally:
            await tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_shutdown_cancels_inflight_cache_load(fake_mcp, monkeypatch):
    tools, entered, exited, gates = fake_mcp
    import focus.mcp.cache as cache
    from focus.config.extensions_config import ExtensionsConfig

    config = ExtensionsConfig.model_validate({"mcpServers": {
        "shared": {"enabled": True, "type": "http", "url": "https://test.invalid/mcp", "description": "test"},
    }})
    monkeypatch.setattr(cache, "get_extensions_config", lambda *args, **kwargs: config)

    async def scenario():
        gates["shared"] = asyncio.Event()
        loading = asyncio.create_task(cache.get_mcp_tools_cached())
        await _until(lambda: bool(entered))
        await tools.close_mcp_sessions()
        await asyncio.gather(loading, return_exceptions=True)
        assert exited == entered
        assert not cache._cache
        assert not cache._loads
        assert not cache._expiry
        assert not tools._MCP_SESSIONS

    asyncio.run(scenario())


def test_cancelling_cached_waiter_preserves_other_run(fake_mcp, monkeypatch):
    tools, entered, exited, gates = fake_mcp
    import focus.mcp.cache as cache
    from focus.config.extensions_config import ExtensionsConfig

    config = ExtensionsConfig.model_validate({"mcpServers": {
        "shared": {"enabled": True, "type": "http", "url": "https://test.invalid/mcp", "description": "test"},
    }})
    monkeypatch.setattr(cache, "get_extensions_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(tools, "MCP_SESSION_IDLE_TIMEOUT", 0.02)

    async def scenario():
        gates["shared"] = asyncio.Event()
        first = asyncio.create_task(cache.get_mcp_tools_cached())
        run = tools.begin_mcp_run()
        second = asyncio.create_task(cache.get_mcp_tools_cached())
        try:
            await _until(lambda: bool(entered))
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            gates["shared"].set()
            loaded = await second
            assert loaded.healthy
            assert entered == ["shared"]
            assert (await cache.get_mcp_tools_cached()) is loaded
            await asyncio.sleep(0.04)
            assert not exited
        finally:
            tools.end_mcp_run(run)
            await _until(lambda: bool(exited))
            await tools.close_mcp_sessions()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["success", "factory_error", "stream_error", "cancel"])
def test_run_worker_releases_connections_on_every_exit(fake_mcp, monkeypatch, outcome):
    tools, _, exited, _ = fake_mcp
    from backend.tests.test_run_events import _RecordingBridge, _governed_context
    from focus.runtime.runs.manager import RunManager
    from focus.runtime.runs.worker import run_agent

    monkeypatch.setattr(tools, "MCP_SESSION_IDLE_TIMEOUT", 0.02)

    async def scenario():
        started = asyncio.Event()
        finish = asyncio.Event()

        class Agent:
            async def astream(self, *args, **kwargs):
                await finish.wait()
                if outcome == "stream_error":
                    raise RuntimeError("stream failed")
                if False:
                    yield None

        async def factory():
            await tools.load_mcp_tools({"shared": {}})
            started.set()
            if outcome == "factory_error":
                raise RuntimeError("assembly failed")
            return Agent()

        manager = RunManager()
        record = manager.create("thread-mcp")
        task = asyncio.create_task(run_agent(
            record=record, bridge=_RecordingBridge(), run_manager=manager,
            app_config=None, graph_input={}, runnable_config={}, agent_factory=factory,
            langgraph_context=_governed_context(record, "workspace", "agent"),
        ))
        try:
            await asyncio.wait_for(started.wait(), 2)
            if outcome != "factory_error":
                await asyncio.sleep(0.04)
                assert not exited
            if outcome == "cancel":
                task.cancel()
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
            await _until(lambda: bool(exited))
            assert not tools._MCP_SESSIONS
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await tools.close_mcp_sessions()

    asyncio.run(scenario())


@pytest.mark.parametrize("stall", [False, True])
def test_real_stdio_process_exits_after_idle_or_initialization_timeout(tmp_path, monkeypatch, stall):
    import focus.mcp.tools as tools
    import mcp.client.stdio as stdio

    processes = []
    create_process = stdio._create_platform_compatible_process

    async def capture_process(**kwargs):
        process = await create_process(**kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(stdio, "_create_platform_compatible_process", capture_process)
    monkeypatch.setattr(tools, "MCP_INITIALIZATION_TIMEOUT", 1.0)
    monkeypatch.setattr(tools, "MCP_SESSION_IDLE_TIMEOUT", 0.02)
    server = tmp_path / "server.py"
    server.write_text('''import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if sys.argv[1] == "stall":
        time.sleep(60)
    result = {}
    if request.get("method") == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "lifecycle", "version": "1"}}
    elif request.get("method") == "tools/list":
        result = {"tools": []}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''', encoding="utf-8")

    async def scenario():
        try:
            result = await tools.load_mcp_tools({"process": {
                "transport": "stdio", "command": sys.executable,
                "args": ["-u", str(server), "stall" if stall else "ready"],
            }})
            assert result.failed_servers == (["process"] if stall else [])
            assert len(processes) == 1
            # 实际 adapter/AnyIO 进程对象必须报告退出，不只检查注册表已清空。
            async with asyncio.timeout(8):
                while processes[0].returncode is None or tools._MCP_SESSIONS:
                    await asyncio.sleep(0.01)
            assert not tools._MCP_CLIENTS
            assert not tools._MCP_CONNECTIONS
        finally:
            await tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_close_shared_initialization(fake_mcp):
    tools, entered, exited, gates = fake_mcp

    async def scenario():
        gates["shared"] = asyncio.Event()
        first = asyncio.create_task(tools.load_mcp_tools({"shared": {}}))
        second = asyncio.create_task(tools.load_mcp_tools({"shared": {}}))
        try:
            while not entered:
                await asyncio.sleep(0)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            gates["shared"].set()
            result = await second
            assert result.healthy
            assert entered == ["shared"]
            assert exited == []
        finally:
            await tools.close_mcp_sessions()

    asyncio.run(scenario())
