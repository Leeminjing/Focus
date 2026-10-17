"""全局/工作区能力隔离与真实 stdio MCP 的刷新、失败重试回归。"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from focus.config.extensions_config import get_extensions_config
from focus.security.effects import ToolEffectKind, effect_of


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def scoped_mcp(tmp_path, monkeypatch):
    import focus.mcp.cache as cache
    import focus.mcp.tools as tools

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / "global"))
    monkeypatch.setattr(cache, "_cache", {})
    monkeypatch.setattr(cache, "_locks", {})
    monkeypatch.setattr(tools, "_MCP_CLIENTS", [])
    monkeypatch.setattr(tools, "_MCP_SESSIONS", [])
    monkeypatch.setattr(tools, "_MCP_CONNECTIONS", {})
    return cache, tools


def test_real_mcp_scopes_refresh_retry_and_trust_isolation(tmp_path, scoped_mcp):
    """使用真实 adapter + 子进程 stdio，不替换 MCP 加载器或连接。"""
    cache, tools = scoped_mcp
    server = tmp_path / "server.py"
    ready = tmp_path / "ready"
    server.write_text('''import json, sys
from pathlib import Path
if not Path(sys.argv[1]).exists():
    sys.exit(1)
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    result = {}
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "scope-test", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "probe", "description": "Return scope", "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": sys.argv[2]}]}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''', encoding="utf-8")

    def config(label):
        return {"enabled": True, "type": "stdio", "description": "scope test",
                "command": sys.executable, "args": ["-u", str(server), str(ready), label]}

    global_config = tmp_path / "global" / "extensions_config.json"
    a, b = tmp_path / "a", tmp_path / "b"
    local = a / ".focus" / "extensions_config.json"
    _write(global_config, {"mcpServers": {"shared": config("global")}})
    _write(local, {"mcpServers": {"shared": config("only-A")}})

    async def scenario():
        try:
            first = await cache.get_mcp_tools_cached(a)
            assert first == []
            assert first.failed_servers == ["shared"]
            assert len(tools._MCP_SESSIONS) == 0
            ready.touch()  # 恢复服务，配置和 mtime 均未改变。
            only_a = await cache.get_mcp_tools_cached(a)
            global_b = await cache.get_mcp_tools_cached(b)
            assert "only-A" in json.dumps(await only_a[0].ainvoke({}))
            assert "global" in json.dumps(await global_b[0].ainvoke({}))
            assert len(tools._MCP_SESSIONS) == 2
            # 同一个连接上的工作区信任覆盖不得修改 B 已冻结的工具。
            local.unlink()
            _write(local, {"mcpServers": {"shared": {"trust": {"*": "no_local_effect"}}}})
            trusted_a = await cache.get_mcp_tools_cached(a, force_refresh=True)
            assert effect_of(trusted_a[0]).kind is ToolEffectKind.NO_LOCAL_EFFECT
            assert effect_of(global_b[0]).kind is ToolEffectKind.OPAQUE_LOCAL
            assert len(tools._MCP_SESSIONS) == 2
            local.unlink()
            assert await cache.get_mcp_tools_cached(a) is global_b
            # 删除全局配置后，不能回退到旧工具。
            global_config.unlink()
            assert await cache.get_mcp_tools_cached(b) == []
        finally:
            await tools.close_mcp_sessions()
        assert not tools._MCP_SESSIONS

    asyncio.run(scenario())


def test_partial_failure_reuses_successful_connection_and_retries(tmp_path, scoped_mcp, monkeypatch):
    from langchain_core.tools import StructuredTool
    import langchain_mcp_adapters.tools as adapters
    import langchain_mcp_adapters.client as clients
    from contextlib import asynccontextmanager

    cache, tools = scoped_mcp
    attempts = []
    recovered = False

    class Client:
        def __init__(self, config):
            self.name = next(iter(config))

        @asynccontextmanager
        async def session(self, name):
            attempts.append(name)
            if name == "flaky" and not recovered:
                raise RuntimeError("offline")
            yield name

    async def load(name):
        return [StructuredTool.from_function(lambda: name, name=name, description="scope test")]

    monkeypatch.setattr(clients, "MultiServerMCPClient", Client)
    monkeypatch.setattr(adapters, "load_mcp_tools", load)
    entry = {"enabled": True, "type": "http", "description": "test", "url": "https://test.invalid/mcp"}
    _write(tmp_path / "global" / "extensions_config.json", {"mcpServers": {"stable": entry, "flaky": entry}})

    async def scenario():
        nonlocal recovered
        try:
            first = await cache.get_mcp_tools_cached()
            assert [tool.name for tool in first] == ["stable"]
            assert not first.healthy
            recovered = True
            second = await cache.get_mcp_tools_cached()
            assert [tool.name for tool in second] == ["stable", "flaky"]
            assert second.healthy
            assert attempts == ["stable", "flaky", "flaky"]
            # 两个并发装配共用同一配置快照。
            loaded = await asyncio.gather(cache.get_mcp_tools_cached(), cache.get_mcp_tools_cached())
            assert loaded == [second, second]
        finally:
            await tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_config_precedence_and_environment_changes(tmp_path, scoped_mcp, monkeypatch):
    cache, _ = scoped_mcp
    default = {"enabled": True, "type": "http", "description": "default", "url": "https://default.test/mcp"}
    _write(tmp_path / "extensions_config.json", {"mcpServers": {"shared": default}})
    _write(tmp_path / "global" / "extensions_config.json", {"mcpServers": {"shared": {"url": "${SCOPED_MCP_URL}"}}})
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a / ".focus" / "extensions_config.json", {"mcpServers": {"shared": {"enabled": False}}})
    monkeypatch.setenv("SCOPED_MCP_URL", "https://global.test/mcp")
    assert not get_extensions_config("extensions_config.json", workspace=a).mcp_servers["shared"].enabled
    assert get_extensions_config("extensions_config.json", workspace=b).mcp_servers["shared"].url == "https://global.test/mcp"
    calls = []

    async def load(*, config):
        calls.append(config.mcp_servers["shared"].url)
        return []

    monkeypatch.setattr(cache, "get_mcp_tools", load)

    async def scenario():
        await cache.get_mcp_tools_cached(b)
        monkeypatch.setenv("SCOPED_MCP_URL", "https://changed.test/mcp")
        await cache.get_mcp_tools_cached(b)
        assert calls == ["https://global.test/mcp", "https://changed.test/mcp"]

    asyncio.run(scenario())
