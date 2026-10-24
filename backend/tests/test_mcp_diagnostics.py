"""MCP 配置失败必须沿工具缓存和桌面刷新结果抵达用户。"""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest


def _write(path, servers):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


@pytest.fixture
def isolated_mcp(tmp_path, monkeypatch):
    import focus.mcp.cache as cache

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / "global"))
    monkeypatch.setattr(cache, "_cache", {})
    monkeypatch.setattr(cache, "_loads", {})
    return cache


def test_all_invalid_configs_are_failed_refreshes(tmp_path, isolated_mcp):
    _write(tmp_path / "global" / "extensions_config.json", {
        "broken": {"enabled": True, "type": "stdio", "description": "missing command"},
    })

    async def scenario():
        tools = await isolated_mcp.get_mcp_tools_cached(force_refresh=True)
        assert tools == []
        assert getattr(tools, "failed_servers", []) == ["broken"]
        assert not tools.healthy
        failure = tools.failures[0]
        assert failure.server_name == "broken"
        assert failure.scope == "global"
        assert failure.config_path == str(tmp_path / "global" / "extensions_config.json")
        assert failure.stage == "configuration"
        assert "command" in failure.reason
        assert "command" in failure.action

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", ["default", "global", "workspace"])
@pytest.mark.parametrize("content", ['{"mcpServers":', '[]', 'null', '"secret-value"', ''])
@pytest.mark.parametrize("force_refresh", [False, True])
def test_broken_config_file_blocks_fallback_and_reports_repair(
    tmp_path, isolated_mcp, fake_servers, scope, content, force_refresh,
):
    from focus.config.extensions_config import get_extensions_config
    import focus.mcp.tools as mcp_tools

    workspace = tmp_path / "workspace"
    paths = {
        "default": tmp_path / "extensions_config.json",
        "global": tmp_path / "global" / "extensions_config.json",
        "workspace": workspace / ".focus" / "extensions_config.json",
    }
    _write(paths["default"], {
        "shared": {"enabled": True, "type": "http", "description": "test", "url": "https://test.invalid"},
    })
    path = paths[scope]
    path.parent.mkdir(parents=True, exist_ok=True)

    async def scenario():
        try:
            # 先建立成功缓存，再损坏文件；错误不能复用之前的工具或回落默认层。
            initial = await isolated_mcp.get_mcp_tools_cached(workspace)
            assert [tool.name for tool in initial] == ["shared"]
            path.write_text(content, encoding="utf-8")
            with pytest.raises(ValueError):
                get_extensions_config("extensions_config.json", workspace=workspace)
            tools = await isolated_mcp.get_mcp_tools_cached(workspace, force_refresh=force_refresh)
            assert tools == [] and not tools.healthy
            failure, = tools.failures
            assert failure.scope == scope
            assert failure.config_path == str(path.resolve())
            assert failure.stage == "configuration"
            assert "JSON" in failure.reason
            assert str(path.resolve()) in failure.action
            assert "修复" in failure.action
            assert "secret-value" not in failure.reason
            if content == '{"mcpServers":':
                assert "第 1 行，第 15 列" in failure.reason
            assert fake_servers[0] == ["shared"]
            _write(path, {"shared": {"enabled": False}})
            repaired = await isolated_mcp.get_mcp_tools_cached(workspace)
            assert repaired == [] and repaired.healthy and repaired.failures == []
        finally:
            await mcp_tools.close_mcp_sessions()

    asyncio.run(scenario())


@pytest.mark.parametrize("entry, reason, action", [
    ({"type": "http"}, "url", "url"),
    ({"type": "sse"}, "url", "url"),
    ({"type": "unknown"}, "transport", "type"),
    ({"type": "stdio", "command": "python", "env": {"KEY": "$FOCUS_MCP_MISSING"}}, "FOCUS_MCP_MISSING", ".env"),
    ({"type": "http", "url": "${FOCUS_MCP_MISSING}"}, "FOCUS_MCP_MISSING", ".env"),
    ({"type": "http", "url": "https://test.invalid", "headers": {"Authorization": "$FOCUS_MCP_MISSING"}}, "FOCUS_MCP_MISSING", ".env"),
    ({"type": "stdio", "command": "$FOCUS_MCP_MISSING"}, "FOCUS_MCP_MISSING", ".env"),
    ({"type": "stdio", "command": "python", "args": ["$FOCUS_MCP_MISSING"]}, "FOCUS_MCP_MISSING", ".env"),
])
def test_configuration_reasons_include_scope_and_action(tmp_path, isolated_mcp, monkeypatch, entry, reason, action):
    monkeypatch.delenv("FOCUS_MCP_MISSING", raising=False)
    config_path = tmp_path / "workspace" / ".focus" / "extensions_config.json"
    _write(config_path, {"broken": {"enabled": True, "description": "test", **entry}})
    tools = asyncio.run(isolated_mcp.get_mcp_tools_cached(tmp_path / "workspace"))
    assert tools.failed_servers == ["broken"]
    assert not tools.healthy
    failure = tools.failures[0]
    assert failure.scope == "workspace"
    assert failure.config_path == str(config_path)
    assert reason in failure.reason
    assert action in failure.action


@pytest.fixture
def fake_servers(monkeypatch):
    import langchain_mcp_adapters.client as clients
    import langchain_mcp_adapters.tools as adapters
    from langchain_core.tools import StructuredTool

    attempts = []
    errors = {}

    class Client:
        def __init__(self, config):
            pass

        @asynccontextmanager
        async def session(self, name):
            attempts.append(name)
            if name in errors:
                raise errors[name]
            yield name

    async def load(name):
        return [StructuredTool.from_function(lambda: name, name=name, description="probe")]

    monkeypatch.setattr(clients, "MultiServerMCPClient", Client)
    monkeypatch.setattr(adapters, "load_mcp_tools", load)
    return attempts, errors


def test_mixed_failures_retain_tools_and_recover(tmp_path, isolated_mcp, fake_servers, monkeypatch):
    import focus.mcp.tools as mcp_tools

    attempts, errors = fake_servers
    entry = {"enabled": True, "type": "http", "description": "test", "url": "https://test.invalid/mcp"}
    path = tmp_path / "global" / "extensions_config.json"
    _write(path, {"stable": entry, "offline": entry, "missing": {**entry, "url": None},
                  "no-key": {**entry, "headers": {"Key": "$FOCUS_MCP_MISSING"}}})
    monkeypatch.delenv("FOCUS_MCP_MISSING", raising=False)
    errors["offline"] = ExceptionGroup("session failed", [ConnectionError("connection refused")])

    async def scenario():
        try:
            first = await isolated_mcp.get_mcp_tools_cached(force_refresh=True)
            assert [tool.name for tool in first] == ["stable"]
            assert set(first.failed_servers) == {"offline", "missing", "no-key"}
            failures = {failure.server_name: failure for failure in first.failures}
            assert failures["offline"].stage == "connection"
            assert "connection refused" in failures["offline"].reason
            assert "网络" in failures["offline"].action
            assert all(f.scope == "global" and f.config_path == str(path) for f in first.failures)
            assert attempts == ["stable", "offline"]  # 配置错误不能进入连接。
            _write(path, {name: entry for name in ("stable", "offline", "missing", "no-key")})
            errors.clear()
            second = await isolated_mcp.get_mcp_tools_cached(force_refresh=True)
            assert len(second) == 4
            assert second.healthy
            assert second.failed_servers == second.failures == []
            assert attempts.count("stable") == 1
        finally:
            await mcp_tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_source_changes_do_not_reuse_wrong_scope_diagnostics(tmp_path, isolated_mcp, fake_servers):
    import focus.mcp.tools as mcp_tools
    from focus.config.extensions_config import get_extensions_config

    _, errors = fake_servers
    errors["shared"] = RuntimeError("offline")
    entry = {"enabled": True, "type": "http", "description": "test", "url": "https://test.invalid"}
    global_path = tmp_path / "global" / "extensions_config.json"
    local_path = tmp_path / "workspace" / ".focus" / "extensions_config.json"
    _write(global_path, {"shared": entry})
    _write(local_path, {"shared": entry})

    async def scenario():
        try:
            local = await isolated_mcp.get_mcp_tools_cached(tmp_path / "workspace")
            assert local.failures[0].scope == "workspace"
            local_path.unlink()
            inherited = await isolated_mcp.get_mcp_tools_cached(tmp_path / "workspace")
            assert inherited.failures[0].scope == "global"
            global_path.unlink()
            _write(tmp_path / "extensions_config.json", {"shared": entry})
            default = get_extensions_config("extensions_config.json")
            assert default.mcp_server_sources["shared"]["scope"] == "default"
        finally:
            await mcp_tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_empty_and_disabled_configs_are_healthy(tmp_path, isolated_mcp):
    async def scenario():
        empty = await isolated_mcp.get_mcp_tools_cached()
        assert empty == [] and empty.healthy and empty.failures == []
        _write(tmp_path / "global" / "extensions_config.json", {
            "disabled": {"enabled": False, "type": "stdio", "description": "missing command"},
        })
        disabled = await isolated_mcp.get_mcp_tools_cached(force_refresh=True)
        assert disabled == [] and disabled.healthy and disabled.failures == []

    asyncio.run(scenario())


@pytest.mark.parametrize("broken_file", [False, True])
def test_refresh_service_serializes_configuration_failure(tmp_path, isolated_mcp, broken_file):
    from backend.app.desktop.composition.service import DesktopService

    _write(tmp_path / "global" / "extensions_config.json", {
        "broken": {"enabled": True, "type": "stdio", "description": "test"},
    })
    if broken_file:
        (tmp_path / "global" / "extensions_config.json").write_text('{"mcpServers":', encoding="utf-8")
    service = DesktopService.__new__(DesktopService)

    @asynccontextmanager
    async def session():
        yield object()

    async def entities(session, task_id):
        return None, SimpleNamespace(path=str(tmp_path / "global"))

    async def skills(task_id):
        return [{"name": "shared", "description": "still available"}]

    service.session_factory = session
    service.workspaces._get_task_entities = entities
    service.capabilities.list_task_skills = skills
    result = asyncio.run(service.refresh_task_capabilities("task"))
    assert result["mcp_tool_count"] == 0
    assert result["failed_servers"] == (["extensions_config.json"] if broken_file else ["broken"])
    assert result["skills"][0]["name"] == "shared"
    assert result["mcp_failures"][0]["scope"] == "global"  # 全局装配不伪装成工作区。
    failure = result["mcp_failures"][0]
    assert ("修复" if broken_file else "command") in failure["action"]
    assert failure["config_path"] == str(tmp_path / "global" / "extensions_config.json")
    if broken_file:
        assert "JSON" in failure["reason"]
    json.dumps(result)


def test_connection_failure_redacts_credentials(tmp_path, isolated_mcp, fake_servers):
    import focus.mcp.tools as mcp_tools

    _, errors = fake_servers
    url = "https://user:password@test.invalid/mcp?token=query-secret"
    _write(tmp_path / "global" / "extensions_config.json", {
        "secret": {"enabled": True, "type": "http", "description": "test", "url": url,
                   "headers": {"Authorization": "Bearer header-secret"}},
    })
    errors["secret"] = RuntimeError(f"401 Unauthorized: {url} Bearer header-secret query-secret password")

    async def scenario():
        try:
            tools = await isolated_mcp.get_mcp_tools_cached()
            reason = tools.failures[0].reason
            assert "401 Unauthorized" in reason
            assert all(secret not in reason for secret in (url, "password", "query-secret", "header-secret"))
        finally:
            await mcp_tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_invalid_server_field_does_not_block_valid_servers(tmp_path, isolated_mcp, fake_servers):
    import focus.mcp.tools as mcp_tools

    _write(tmp_path / "global" / "extensions_config.json", {
        "stable": {"enabled": True, "type": "http", "description": "test", "url": "https://test.invalid"},
        "invalid": {"enabled": True, "type": "stdio", "description": "test", "command": 123},
        "incomplete": {"enabled": True},
    })

    async def scenario():
        try:
            tools = await isolated_mcp.get_mcp_tools_cached(force_refresh=True)
            assert [tool.name for tool in tools] == ["stable"]
            assert set(tools.failed_servers) == {"invalid", "incomplete"}
            assert all(f.stage == "configuration" and f.scope == "global" for f in tools.failures)
            assert "command" in next(f.reason for f in tools.failures if f.server_name == "invalid")
        finally:
            await mcp_tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_setting_missing_environment_variable_allows_retry(tmp_path, isolated_mcp, fake_servers, monkeypatch):
    import focus.mcp.tools as mcp_tools

    monkeypatch.delenv("FOCUS_MCP_MISSING", raising=False)
    _write(tmp_path / "global" / "extensions_config.json", {
        "keyed": {"enabled": True, "type": "http", "description": "test", "url": "https://test.invalid",
                  "headers": {"Key": "$FOCUS_MCP_MISSING"}},
    })

    async def scenario():
        try:
            failed = await isolated_mcp.get_mcp_tools_cached(force_refresh=True)
            assert failed.failed_servers == ["keyed"]
            monkeypatch.setenv("FOCUS_MCP_MISSING", "configured")
            recovered = await isolated_mcp.get_mcp_tools_cached(force_refresh=True)
            assert [tool.name for tool in recovered] == ["keyed"]
            assert recovered.healthy and recovered.failures == []
        finally:
            await mcp_tools.close_mcp_sessions()

    asyncio.run(scenario())


def test_missing_adapter_produces_actionable_failure(tmp_path, isolated_mcp, monkeypatch):
    _write(tmp_path / "global" / "extensions_config.json", {
        "unavailable": {"enabled": True, "type": "http", "description": "test", "url": "https://test.invalid"},
    })
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", None)
    tools = asyncio.run(isolated_mcp.get_mcp_tools_cached(force_refresh=True))
    assert tools.failed_servers == ["unavailable"]
    assert tools.failures[0].scope == "global"
    assert "langchain-mcp-adapters" in tools.failures[0].reason
    assert "安装" in tools.failures[0].action
    assert not tools.healthy


def test_stdio_connection_failure_suggests_executable_repair(tmp_path, isolated_mcp, fake_servers):
    import focus.mcp.tools as mcp_tools

    _, errors = fake_servers
    errors["stdio"] = FileNotFoundError("executable not found")
    _write(tmp_path / "global" / "extensions_config.json", {
        "stdio": {"enabled": True, "type": "stdio", "description": "test", "command": "missing"},
    })

    async def scenario():
        try:
            tools = await isolated_mcp.get_mcp_tools_cached()
            failure = tools.failures[0]
            assert failure.stage == "connection"
            assert "executable not found" in failure.reason
            assert "command" in failure.action
        finally:
            await mcp_tools.close_mcp_sessions()

    asyncio.run(scenario())
