r"""本文件提供配置聚合、MCP 内容缓存与全局配置家目录访问策略测试。

输入为隔离的配置文件、固定 mtime、路径访问请求；输出为配置优先级、缓存刷新和访问决策断言。
具体工作流为在临时目录构造两层配置，固定文件时间因果后调用公开读取/cache 接口，并验证同一快照和变更快照的行为。
访问用例经生产执行身份派生策略，分别验证只读拒写、工作区写边界和全局业务审批，避免缺省只读被误当成写授权。
示例：`pytest backend/tests/test_assembly_config.py -q`。
"""

import json
import os
import threading
from pathlib import Path

import pytest

from focus.config.layered import (
    global_home,
    layered_mtime,
    load_layered_map,
)
from focus.security import (
    AccessDecision,
    AccessOperation,
    canonical_target,
    decide_path_access,
    policy_from_context,
)
from focus.security.context import AuthorizationIdentity, ExecutionProfile, RoutingIdentity, derive_security_context
from focus.security.policy import AccessMode, workspace_roots

_GLOBAL_ENV = "FOCUS_GLOBAL_HOME"


@pytest.fixture
def global_home_dir(tmp_path, monkeypatch):
    """把全局态 `.focus` 重定向到临时目录，便于测试。"""
    home = tmp_path / ".focus"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(_GLOBAL_ENV, str(home))
    return home


def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_global_home_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv(_GLOBAL_ENV, str(tmp_path / "g"))
    assert global_home() == (tmp_path / "g").resolve()


def test_load_layered_map_user_preference_wins(global_home_dir, tmp_path):
    file_layer = tmp_path / "extensions_config.json"
    _write(global_home_dir / "extensions_config.json", {"mcpServers": {"a": {"enabled": True, "type": "http", "url": "g"}}})
    _write(file_layer, {"mcpServers": {"a": {"enabled": False, "type": "http", "url": "r"}, "b": {"enabled": True, "type": "http", "url": "rb"}}})
    merged = load_layered_map("extensions_config.json", str(file_layer))
    servers = merged["mcpServers"]
    # 用户偏好层优先：a 被用户偏好层覆盖；b 仅文件层有，保留
    assert servers["a"]["url"] == "g"
    assert servers["b"]["url"] == "rb"


def test_load_layered_map_file_layer_supplies_unset_keys(global_home_dir, tmp_path):
    file_layer = tmp_path / "config.yaml"
    file_layer.write_text("models: []\n", encoding="utf-8")
    (global_home_dir / "config.yaml").write_text("commitment:\n  enabled: true\n", encoding="utf-8")
    merged = load_layered_map("config.yaml", str(file_layer))
    # 两层各自的键都保留：用户偏好层补 commitment，文件层补 models
    assert merged["commitment"]["enabled"] is True
    assert merged["models"] == []


def test_layered_mtime_max(global_home_dir, tmp_path):
    repo = tmp_path / "extensions_config.json"
    _write(global_home_dir / "extensions_config.json", {"mcpServers": {}})
    _write(repo, {"mcpServers": {}})
    mt = layered_mtime("extensions_config.json", str(repo))
    assert mt is not None
    assert mt == max((global_home_dir / "extensions_config.json").stat().st_mtime, repo.stat().st_mtime)


def test_layered_mtime_none():
    assert layered_mtime("nope.json", "nope.json") is None


def _assembly_policy(workspace, mode, *, allow_global_config=False):
    profile = ExecutionProfile(
        authorization=AuthorizationIdentity(workspace=workspace,
            roots=workspace_roots(workspace, allow_global_config=allow_global_config),
            permissions=("read", "write"), access_mode=mode, agent_role="main"),
        routing=RoutingIdentity(thread_id="assembly-thread", workspace_id="assembly-workspace",
                                agent_id="main:assembly", task_id="assembly", checkpoint_ns=""),
    )
    return policy_from_context(derive_security_context(profile).to_runtime_context())


def test_containment_allows_global_root(global_home_dir, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    global_file = global_home_dir / "plugins" / "demo" / "plugin.py"
    plain = _assembly_policy(workspace, AccessMode.DANGER_FULL_ACCESS)

    inside = canonical_target(workspace, "a.txt")
    assert inside == (workspace / "a.txt").resolve()
    assert decide_path_access(plain, inside, AccessOperation.WRITE) is AccessDecision.ALLOW

    outside = canonical_target(workspace, str(global_file))
    assert decide_path_access(plain, outside, AccessOperation.WRITE) is AccessDecision.ASK

    read_only = _assembly_policy(workspace, AccessMode.READ_ONLY)
    assert decide_path_access(read_only, inside, AccessOperation.WRITE) is AccessDecision.DENY
    assert decide_path_access(read_only, outside, AccessOperation.WRITE) is AccessDecision.DENY
    limited = _assembly_policy(workspace, AccessMode.WORKSPACE_WRITE)
    assert decide_path_access(limited, inside, AccessOperation.WRITE) is AccessDecision.ALLOW
    ordinary_outside = Path.home() / "focus-policy-test-only" / "outside.txt"
    assert decide_path_access(limited, ordinary_outside, AccessOperation.WRITE) is AccessDecision.DENY
    assert global_home().resolve() not in limited.roots

    scoped = _assembly_policy(workspace, AccessMode.WORKSPACE_WRITE, allow_global_config=True)
    assert global_home().resolve() in scoped.roots
    scratch = canonical_target(workspace, str(global_home_dir / "scratch" / "note.txt"))
    assert decide_path_access(scoped, scratch, AccessOperation.WRITE) is AccessDecision.ALLOW
    assert decide_path_access(scoped, outside, AccessOperation.WRITE) is AccessDecision.ASK


def test_assembly_prompt_constants():
    from backend.app.desktop.service import _ASSEMBLY_SYSTEM_PROMPT, _MAIN_SYSTEM_PROMPT

    assert _ASSEMBLY_SYSTEM_PROMPT != _MAIN_SYSTEM_PROMPT
    assert "~/.focus" in _ASSEMBLY_SYSTEM_PROMPT


def test_assembly_run_sets_allow_global_config():
    """装配运行把全局配置家目录并入工作根：装配位显式传 allow_global_config。"""
    import inspect

    from backend.app.desktop.service import DesktopService

    assert "allow_global_config" in inspect.signature(DesktopService._prepare).parameters
    assert "allow_global_config=is_global_assembly" in inspect.getsource(DesktopService.start_main_run)


def test_regression_global_empty_equals_repo(tmp_path, monkeypatch):
    """全局态为空(single-layer)时，聚合结果应与仓库态一致（回归：不改变现状）。"""
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / ".focus"))
    repo = tmp_path / "config.yaml"
    repo.write_text("models: []\n", encoding="utf-8")
    merged = load_layered_map("config.yaml", str(repo))
    assert merged == {"models": []}


def _write_plugin(plugins_root: Path, name: str) -> None:
    pdir = plugins_root / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.json").write_text(
        '{"name":"%s","version":"1.0.0","enabled":true,"provides":["tool","hook.before_model"],"entry":"plugin.py"}'
        % name,
        encoding="utf-8",
    )
    (pdir / "plugin.py").write_text(
        "from langchain_core.tools import tool\n"
        "from focus.plugins.schemas import PluginDeclaration\n"
        "@tool\n"
        "def ping(text: str) -> str:\n"
        '    """Ping the plugin."""\n'
        '    return "pong:"+text\n'
        "def before_model(state, runtime):\n"
        '    return {"title": state.get("title")}\n'
        "def build_plugin(context):\n"
        '    return PluginDeclaration(tools=[ping], hooks={"hook.before_model":[before_model]})\n',
        encoding="utf-8",
    )


def test_plugin_reload_active(monkeypatch, tmp_path):
    """写入新插件 → reload_plugins → 状态 active、injected 正确、tools/hooks 可见。"""
    from focus.plugins import reload_plugins

    home = tmp_path / ".focus"
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(home))
    plugins_root = home / "plugins"
    _write_plugin(plugins_root, "verify-demo")
    registry = reload_plugins(str(plugins_root))
    plugin = next(p for p in registry.list_plugins() if p["name"] == "verify-demo")
    assert plugin["status"] == "active"
    assert "tool" in plugin["injected"]
    assert "hook.before_model" in plugin["injected"]
    assert len(registry.tools()) >= 1
    assert any(h.plugin == "verify-demo" for h in registry.hooks("hook.before_model"))


def _server(url):
    return {"enabled": True, "type": "http", "description": "test", "url": url}


def test_mcp_cache_refresh_on_content_change(monkeypatch, tmp_path):
    import asyncio
    import focus.mcp.cache as cache

    monkeypatch.chdir(tmp_path)
    home = tmp_path / ".focus"
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(home))
    config_file = _write(home / "extensions_config.json", {"mcpServers": {"a": _server("https://old.test/mcp")}})
    calls = []

    async def fake_get_tools(*, config):
        calls.append(config.mcp_servers["a"].url)
        return []

    monkeypatch.setattr(cache, "get_mcp_tools", fake_get_tools)
    monkeypatch.setattr(cache, "_cache", {})
    monkeypatch.setattr(cache, "_locks", {})

    async def scenario():
        await cache.get_mcp_tools_cached()
        await cache.get_mcp_tools_cached()
        assert len(calls) == 1
        previous_ns = config_file.stat().st_mtime_ns
        _write(config_file, {"mcpServers": {"a": _server("https://new.test/mcp")}})
        # 文件时间未变也必须刷新；不依赖文件系统时间精度。
        os.utime(config_file, ns=(previous_ns, previous_ns))
        await cache.get_mcp_tools_cached()
        assert calls == ["https://old.test/mcp", "https://new.test/mcp"]

    asyncio.run(scenario())


def test_mcp_cache_refreshes_after_newer_global_file_is_deleted(monkeypatch, tmp_path):
    import asyncio
    import focus.mcp.cache as cache

    monkeypatch.chdir(tmp_path)
    home = tmp_path / ".focus"
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(home))
    repo = _write(tmp_path / "extensions_config.json", {"mcpServers": {"a": _server("https://default.test/mcp")}})
    user = _write(home / "extensions_config.json", {"mcpServers": {"a": _server("https://global.test/mcp")}})
    os.utime(user, (repo.stat().st_mtime + 10, repo.stat().st_mtime + 10))
    calls = []

    async def fake_get_tools(*, config):
        calls.append(config.mcp_servers["a"].url)
        return []

    monkeypatch.setattr(cache, "get_mcp_tools", fake_get_tools)
    monkeypatch.setattr(cache, "_cache", {})
    monkeypatch.setattr(cache, "_locks", {})

    async def scenario():
        await cache.get_mcp_tools_cached()
        user.unlink()
        await cache.get_mcp_tools_cached()
        assert calls == ["https://global.test/mcp", "https://default.test/mcp"]

    asyncio.run(scenario())
