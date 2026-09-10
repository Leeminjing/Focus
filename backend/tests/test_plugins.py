"""插件系统单元测试：接口目录、声明校验、注册表（冲突/依赖/撤销/顺序）、桥接分发、装配集成。"""

import asyncio
import json
import shutil
from pathlib import Path

from langchain_core.tools import StructuredTool, tool

import pytest

from focus.plugins import get_plugin_registry
from focus.plugins.bridge import PluginBridgeMiddleware
from focus.plugins.interfaces import InterfaceCatalog, PluginInterface, builtin_catalog
from focus.plugins.registry import PluginRegistry
from focus.plugins.schemas import PluginDeclaration, PluginManifest


def _catalog(*extra: PluginInterface) -> InterfaceCatalog:
    base = builtin_catalog()
    return InterfaceCatalog(tuple(base.get(name) for name in base.names) + extra)


_SINGLE_STORAGE = PluginInterface(
    name="service.primary-storage", cardinality="single", mutability="mutable",
)
_MULTI_ATTACHMENT = PluginInterface(
    name="service.attachment", cardinality="multi", mutability="mutable",
)
_FAST_BEFORE_MODEL = PluginInterface(
    name="hook.before_model", cardinality="multi", mutability="mutable",
    failure_policy="skip", timeout_seconds=0.05,
)


def _manifest(name: str, provides=(), requires=(), version: str = "1.0.0") -> PluginManifest:
    return PluginManifest(name=name, version=version, provides=list(provides), requires=list(requires))


@tool
def plugin_browser(url: str) -> str:
    """测试插件工具：返回传入的 url。"""
    return f"browser:{url}"


# === 1.3 声明校验 ===


def test_unknown_provides_interface_rejected():
    registry = PluginRegistry(builtin_catalog())
    record = registry.register(_manifest("p1", provides=["hook.unknown_point"]), PluginDeclaration())
    assert record.status == "rejected"
    assert "Unsupported Extension Interface: hook.unknown_point" in record.reason


def test_declared_but_missing_implementation_rejected():
    registry = PluginRegistry(builtin_catalog())
    record = registry.register(_manifest("p1", provides=["hook.after_model"]), PluginDeclaration())
    assert record.status == "rejected"
    assert "接口不符: hook.after_model" in record.reason


def test_undeclared_implementation_ignored():
    registry = PluginRegistry(builtin_catalog())
    declaration = PluginDeclaration(
        tools=[plugin_browser],
        hooks={"hook.before_model": [lambda state, runtime: None]},
    )
    record = registry.register(_manifest("p1", provides=["tool"]), declaration)
    registry.resolve_dependencies()
    assert record.status == "active"
    assert record.injected == ["tool"]
    assert registry.tools() == [plugin_browser]
    assert registry.hooks("hook.before_model") == []


def test_requires_unknown_interface_unavailable():
    registry = PluginRegistry(builtin_catalog())
    record = registry.register(
        _manifest("p1", provides=["tool"], requires=["service.not-defined"]),
        PluginDeclaration(tools=[plugin_browser]),
    )
    assert record.status == "unavailable"
    assert "Missing dependency: service.not-defined" in record.reason
    assert registry.tools() == []


# === 2.4 注册表 ===


def test_hook_order_stable_by_registration():
    registry = PluginRegistry(builtin_catalog())
    for name in ("b", "a", "c"):
        registry.register(
            _manifest(name, provides=["hook.before_model"]),
            PluginDeclaration(hooks={"hook.before_model": [lambda state, runtime, n=name: None]}),
        )
    registry.resolve_dependencies()
    assert [impl.plugin for impl in registry.hooks("hook.before_model")] == ["b", "a", "c"]


def test_single_interface_conflict_rejects_whole_plugin():
    registry = PluginRegistry(_catalog(_SINGLE_STORAGE))
    registry.register(
        _manifest("storage-a", provides=["service.primary-storage"]),
        PluginDeclaration(services={"service.primary-storage": object()}),
    )
    registry.resolve_dependencies()
    conflict_record = registry.register(
        _manifest("storage-b", provides=["service.primary-storage", "hook.before_model"]),
        PluginDeclaration(
            services={"service.primary-storage": object()},
            hooks={"hook.before_model": [lambda state, runtime: None]},
        ),
    )
    registry.resolve_dependencies()
    assert conflict_record.status == "rejected"
    assert conflict_record.conflict == {
        "interface": "service.primary-storage", "current": "storage-a", "new": "storage-b",
    }
    # 注入原子性：冲突插件的其他实现也不进入系统
    assert registry.hooks("hook.before_model") == []
    assert registry.service("service.primary-storage") is not None


def test_dependency_resolved_by_any_provider_regardless_of_load_order():
    registry = PluginRegistry(_catalog(_MULTI_ATTACHMENT))
    registry.register(
        _manifest("vision", provides=["tool"], requires=["service.attachment"]),
        PluginDeclaration(tools=[plugin_browser]),
    )
    registry.register(
        _manifest("remote-attach", provides=["service.attachment"]),
        PluginDeclaration(services={"service.attachment": "remote-impl"}),
    )
    registry.resolve_dependencies()
    records = {item["name"]: item for item in registry.list_plugins()}
    assert records["vision"]["status"] == "active"
    assert records["vision"]["injected"] == ["tool"]


def test_dependency_missing_makes_plugin_unavailable_without_injection():
    registry = PluginRegistry(_catalog(_MULTI_ATTACHMENT))
    registry.register(
        _manifest("vision", provides=["tool"], requires=["service.attachment"]),
        PluginDeclaration(tools=[plugin_browser]),
    )
    registry.resolve_dependencies()
    records = {item["name"]: item for item in registry.list_plugins()}
    assert records["vision"]["status"] == "unavailable"
    assert records["vision"]["missing"] == ["service.attachment"]
    assert registry.tools() == []


def test_multi_service_aggregates_and_dependent_sees_all():
    registry = PluginRegistry(_catalog(_MULTI_ATTACHMENT))
    for name in ("local", "remote"):
        registry.register(
            _manifest(name, provides=["service.attachment"]),
            PluginDeclaration(services={"service.attachment": name}),
        )
    registry.resolve_dependencies()
    assert registry.service("service.attachment") == ["local", "remote"]


def test_revocation_removes_only_that_plugin(tmp_path):
    root = tmp_path / "plugins"
    for name in ("b-plugin", "a-plugin"):
        plugin_dir = root / name
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(
            json.dumps({
                "name": name, "version": "1.0.0", "enabled": True,
                "provides": ["tool"], "entry": "plugin.py",
            }), encoding="utf-8",
        )
        (plugin_dir / "plugin.py").write_text(
            "from langchain_core.tools import tool\n"
            "from focus.plugins.schemas import PluginDeclaration\n"
            f"@tool\ndef tool_{name.replace('-', '_')}(x: str) -> str:\n"
            f"    '''{name} 工具。'''\n    return x\n"
            f"def build_plugin(context):\n"
            f"    return PluginDeclaration(tools=[tool_{name.replace('-', '_')}])\n",
            encoding="utf-8",
        )
    registry = get_plugin_registry(root=root)
    assert [tool_.name for tool_ in registry.tools()] == ["tool_a_plugin", "tool_b_plugin"]
    # 撤销 B：关闭其启用开关后重新加载，仅 B 消失
    manifest_file = root / "b-plugin" / "plugin.json"
    manifest_file.write_text(
        json.dumps({"name": "b-plugin", "version": "1.0.0", "enabled": False, "provides": ["tool"]}),
        encoding="utf-8",
    )
    reloaded = PluginRegistry(builtin_catalog())
    from focus.plugins.loader import load_plugins

    load_plugins(reloaded, root)
    assert [tool_.name for tool_ in reloaded.tools()] == ["tool_a_plugin"]


def test_build_failure_marks_unavailable_without_blocking_others(tmp_path):
    root = tmp_path / "plugins"
    good = root / "good-plugin"
    good.mkdir(parents=True)
    (good / "plugin.json").write_text(
        json.dumps({"name": "good-plugin", "version": "1", "provides": ["tool"], "entry": "plugin.py"}),
        encoding="utf-8",
    )
    (good / "plugin.py").write_text(
        "from langchain_core.tools import tool\n"
        "from focus.plugins.schemas import PluginDeclaration\n"
        "@tool\ndef good_tool(x: str) -> str:\n    '''good。'''\n    return x\n"
        "def build_plugin(context):\n    return PluginDeclaration(tools=[good_tool])\n",
        encoding="utf-8",
    )
    bad = root / "bad-plugin"
    bad.mkdir(parents=True)
    (bad / "plugin.json").write_text(
        json.dumps({"name": "bad-plugin", "version": "1", "provides": ["tool"], "entry": "plugin.py"}),
        encoding="utf-8",
    )
    (bad / "plugin.py").write_text(
        "from focus.plugins.schemas import PluginDeclaration\n"
        "def build_plugin(context):\n    raise RuntimeError('Invalid configuration: missing api key')\n",
        encoding="utf-8",
    )
    registry = PluginRegistry(builtin_catalog())
    from focus.plugins.loader import load_plugins

    load_plugins(registry, root)
    records = {item["name"]: item for item in registry.list_plugins()}
    assert records["bad-plugin"]["status"] == "unavailable"
    assert "Invalid configuration" in records["bad-plugin"]["reason"]
    assert records["good-plugin"]["status"] == "active"
    assert [tool_.name for tool_ in registry.tools()] == ["good_tool"]


def test_empty_plugin_dir_zero_impact(tmp_path):
    registry = get_plugin_registry(root=tmp_path / "nonexistent")
    assert registry.tools() == []
    assert registry.list_plugins() == []


def test_trace_ring_buffer_evicts_oldest():
    registry = PluginRegistry(builtin_catalog(), trace_maxlen=2)
    registry.trace("hook.before_model", "a", "success", 1.0)
    registry.trace("hook.before_model", "b", "failed", 2.0, "boom")
    registry.trace("hook.before_model", "c", "timeout", 3.0, "slow")
    traces = registry.traces()
    assert len(traces) == 2
    assert [item["plugin"] for item in traces] == ["c", "b"]


# === 3.3 桥接分发 ===


def test_bridge_tools_snapshot_registry():
    registry = PluginRegistry(builtin_catalog())
    registry.register(_manifest("p1", provides=["tool"]), PluginDeclaration(tools=[plugin_browser]))
    registry.resolve_dependencies()
    bridge = PluginBridgeMiddleware(registry)
    assert bridge.tools == [plugin_browser]


def test_before_model_pipeline_later_sees_earlier_changes():
    async def run():
        registry = PluginRegistry(builtin_catalog())

        async def hook_a(state, runtime):
            return {"messages": state["messages"] + ["A"]}

        async def hook_b(state, runtime):
            return {"messages": state["messages"] + ["B"]}

        registry.register(
            _manifest("a", provides=["hook.before_model"]),
            PluginDeclaration(hooks={"hook.before_model": [hook_a]}),
        )
        registry.register(
            _manifest("b", provides=["hook.before_model"]),
            PluginDeclaration(hooks={"hook.before_model": [hook_b]}),
        )
        registry.resolve_dependencies()
        bridge = PluginBridgeMiddleware(registry)
        diff = await bridge.abefore_model({"messages": ["origin"]}, None)
        # B 看到 A 的修改：B 的返回值包含 ["origin", "A", "B"]；最终 diff 合并
        assert diff["messages"] == ["origin", "A", "B"]
        traces = registry.traces()
        assert [item["plugin"] for item in traces] == ["b", "a"]
        assert all(item["status"] == "success" for item in traces)

    asyncio.run(run())


def test_after_model_readonly_discards_return_value():
    async def run():
        registry = PluginRegistry(builtin_catalog())
        seen = []

        async def observer(state, runtime):
            seen.append(state)
            return {"messages": "should-be-discarded"}

        registry.register(
            _manifest("obs", provides=["hook.after_model"]),
            PluginDeclaration(hooks={"hook.after_model": [observer]}),
        )
        registry.resolve_dependencies()
        bridge = PluginBridgeMiddleware(registry)
        result = await bridge.aafter_model({"messages": ["keep"]}, None)
        assert result is None
        assert seen == [{"messages": ["keep"]}]

    asyncio.run(run())


def test_timeout_records_extension_timeout_and_continues():
    async def run():
        registry = PluginRegistry(_catalog(_FAST_BEFORE_MODEL))

        async def slow(state, runtime):
            await asyncio.sleep(1)
            return {"messages": ["slow"]}

        async def fast(state, runtime):
            return {"messages": state["messages"] + ["fast"]}

        registry.register(
            _manifest("slow-p", provides=["hook.before_model"]),
            PluginDeclaration(hooks={"hook.before_model": [slow]}),
        )
        registry.register(
            _manifest("fast-p", provides=["hook.before_model"]),
            PluginDeclaration(hooks={"hook.before_model": [fast]}),
        )
        registry.resolve_dependencies()
        bridge = PluginBridgeMiddleware(registry)
        diff = await bridge.abefore_model({"messages": ["origin"]}, None)
        assert diff["messages"] == ["origin", "fast"]
        traces = registry.traces()
        assert traces[1]["status"] == "timeout"
        assert "Extension Timeout: Plugin=slow-p, Interface=hook.before_model" in traces[1]["error"]
        assert traces[0]["plugin"] == "fast-p"

    asyncio.run(run())


def test_sync_callback_runs_via_thread():
    async def run():
        registry = PluginRegistry(builtin_catalog())
        seen = []

        def sync_hook(state, runtime):
            seen.append(state["messages"])
            return {"messages": state["messages"] + ["sync"]}

        registry.register(
            _manifest("sync-p", provides=["hook.before_model"]),
            PluginDeclaration(hooks={"hook.before_model": [sync_hook]}),
        )
        registry.resolve_dependencies()
        bridge = PluginBridgeMiddleware(registry)
        diff = await bridge.abefore_model({"messages": ["origin"]}, None)
        assert diff["messages"] == ["origin", "sync"]
        assert seen == [["origin"]]

    asyncio.run(run())


def test_wrap_tool_call_before_rewrites_after_observes():
    async def run():
        registry = PluginRegistry(builtin_catalog())
        observed = []

        def before_tool(request):
            return request.model_copy(update={"tool_call": {**request.tool_call, "args": {"url": "https://rewritten"}}})

        def after_tool(request, result):
            observed.append(result.content)

        registry.register(
            _manifest("toolkit", provides=["hook.before_tool", "hook.after_tool"]),
            PluginDeclaration(hooks={
                "hook.before_tool": [before_tool],
                "hook.after_tool": [after_tool],
            }),
        )
        registry.resolve_dependencies()
        bridge = PluginBridgeMiddleware(registry)

        class FakeRequest:
            tool_call = {"name": "plugin_browser", "args": {"url": "https://original"}, "id": "c1"}

            def model_copy(self, update):
                copied = FakeRequest()
                copied.tool_call = update["tool_call"]
                return copied

        async def handler(request):
            from langchain_core.messages import ToolMessage

            assert request.tool_call["args"]["url"] == "https://rewritten"
            return ToolMessage(content="page-content", tool_call_id="c1")

        result = await bridge.awrap_tool_call(FakeRequest(), handler)
        assert result.content == "page-content"
        assert observed == ["page-content"]

    asyncio.run(run())


def test_empty_registry_bridge_is_noop():
    async def run():
        bridge = PluginBridgeMiddleware(PluginRegistry(builtin_catalog()))
        assert bridge.tools == []
        assert await bridge.abefore_agent({"messages": []}, None) is None
        assert await bridge.abefore_model({"messages": []}, None) is None

    asyncio.run(run())


# === 装配集成 ===


def test_make_lead_agent_appends_bridge_and_injects_plugin_tool(monkeypatch, tmp_path):
    async def run():
        current = {"registry": PluginRegistry(builtin_catalog())}
        current["registry"].register(
            _manifest("p1", provides=["tool"]), PluginDeclaration(tools=[plugin_browser]),
        )
        current["registry"].resolve_dependencies()
        monkeypatch.setattr("focus.agents.lead.agent.get_plugin_registry", lambda: current["registry"])

        from focus.agents.lead import make_lead_agent

        agent = await make_lead_agent(
            model_name=None,
            tools=[], system_prompt="", middlewares=[],
            app_config=_fake_app_config(),
        )
        tools_node = agent.nodes.get("tools")
        assert tools_node is not None
        assert "plugin_browser" in tools_node.bound.tools_by_name
        # 桥接位于最终链末端：空注册表时工具节点无插件工具
        current["registry"] = PluginRegistry(builtin_catalog())
        agent_empty = await make_lead_agent(
            model_name=None,
            tools=[], system_prompt="", middlewares=[],
            app_config=_fake_app_config(),
        )
        assert "plugin_browser" not in agent_empty.nodes["tools"].bound.tools_by_name

    asyncio.run(run())


def test_name_collision_system_tool_wins(monkeypatch):
    async def run():
        sys_tool = StructuredTool.from_function(
            func=lambda x: "sys", name="browser", description="system tool",
        )
        plugin_dup = StructuredTool.from_function(
            func=lambda x: "plugin", name="browser", description="plugin tool",
        )
        registry = PluginRegistry(builtin_catalog())
        registry.register(_manifest("dup", provides=["tool"]), PluginDeclaration(tools=[plugin_dup]))
        registry.resolve_dependencies()
        monkeypatch.setattr("focus.agents.lead.agent.get_plugin_registry", lambda: registry)

        from focus.agents.lead import make_lead_agent

        agent = await make_lead_agent(
            model_name=None, tools=[sys_tool], system_prompt="", middlewares=[],
            app_config=_fake_app_config(),
        )
        by_name = agent.nodes["tools"].bound.tools_by_name
        assert by_name["browser"].description == "system tool"

    asyncio.run(run())


# === 6. 进程外插件（跨语言，决策 9/10） ===

_REMOTE_RUNTIME = r"""
import json
import sys


def reply(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)


for line in sys.stdin:
    msg = json.loads(line)
    kind = msg["type"]
    if kind == "hello":
        reply({
            "type": "hello", "id": msg["id"],
            "tools": [{
                "name": "remote_echo",
                "description": "remote echo tool",
                "schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            }],
            "hooks": ["hook.before_model", "hook.after_model", "hook.before_tool", "hook.after_tool"],
        })
    elif kind == "tool_call":
        reply({"type": "tool_result", "id": msg["id"], "content": f"remote:{msg['args'].get('text')}"})
    elif kind == "hook":
        iface = msg["interface"]
        if iface == "hook.before_model":
            messages = msg["payload"]["state"].get("messages", [])
            reply({"type": "hook_result", "id": msg["id"],
                   "updates": {"messages": messages + [{"role": "human", "content": "remote-added"}]}})
        elif iface == "hook.before_tool":
            call = dict(msg["payload"]["tool_call"])
            call["args"] = {**call.get("args", {}), "text": "remote-rewritten"}
            reply({"type": "hook_result", "id": msg["id"], "updates": {"tool_call": call}})
        else:
            reply({"type": "hook_result", "id": msg["id"], "updates": None})
"""


def _remote_plugin(tmp_path: Path, command: list[str], hooks: list[str] | None = None) -> PluginRegistry:
    root = tmp_path / "plugins"
    plugin_dir = root / "remote-p"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({
            "name": "remote-p", "version": "1.0.0", "enabled": True,
            "provides": ["tool", *(hooks or ["hook.before_model"])],
            "runtime": {"command": command[0], "args": command[1:]},
        }), encoding="utf-8",
    )
    (plugin_dir / "runtime.py").write_text(_REMOTE_RUNTIME, encoding="utf-8")
    registry = PluginRegistry(builtin_catalog())
    from focus.plugins.loader import load_plugins

    load_plugins(registry, root)
    return registry


def _python_runtime(tmp_path: Path) -> list[str]:
    import sys

    return [sys.executable, str(tmp_path / "plugins" / "remote-p" / "runtime.py")]


def test_manifest_runtime_and_entry_are_mutually_exclusive():
    with pytest.raises(Exception):
        PluginManifest(name="x", version="1", entry="custom.py", runtime={"command": "node", "args": []})


def test_remote_plugin_hello_and_tool_roundtrip(tmp_path):
    registry = _remote_plugin(tmp_path, _python_runtime(tmp_path))
    records = {item["name"]: item for item in registry.list_plugins()}
    assert records["remote-p"]["status"] == "active"
    assert [tool_.name for tool_ in registry.tools()] == ["remote_echo"]

    async def run():
        result = await registry.tools()[0].ainvoke({"text": "hi"})
        assert result == "remote:hi"

    asyncio.run(run())


def test_remote_hook_before_model_roundtrip(tmp_path):
    registry = _remote_plugin(tmp_path, _python_runtime(tmp_path))

    async def run():
        bridge = PluginBridgeMiddleware(registry)
        diff = await bridge.abefore_model({"messages": ["origin"]}, None)
        assert diff["messages"] == ["origin", {"role": "human", "content": "remote-added"}]
        # 只读接口返回值被丢弃
        assert await bridge.aafter_model({"messages": ["origin"]}, None) is None

    asyncio.run(run())


def test_remote_before_tool_rewrites_request(tmp_path):
    registry = _remote_plugin(
        tmp_path, _python_runtime(tmp_path),
        hooks=["hook.before_tool", "hook.after_tool"],
    )

    async def run():
        bridge = PluginBridgeMiddleware(registry)

        class FakeRequest:
            tool_call = {"name": "remote_echo", "args": {"text": "original"}, "id": "c1"}

            def model_copy(self, update):
                copied = FakeRequest()
                copied.tool_call = update["tool_call"]
                return copied

        seen = {}

        async def handler(request):
            seen["args"] = request.tool_call["args"]
            from langchain_core.messages import ToolMessage

            return ToolMessage(content="ok", tool_call_id="c1")

        result = await bridge.awrap_tool_call(FakeRequest(), handler)
        assert seen["args"] == {"text": "remote-rewritten"}
        assert result.content == "ok"

    asyncio.run(run())


def test_remote_missing_command_unavailable_others_unaffected(tmp_path):
    root = tmp_path / "plugins"
    good = root / "good-plugin"
    good.mkdir(parents=True)
    (good / "plugin.json").write_text(
        json.dumps({"name": "good-plugin", "version": "1", "provides": ["tool"], "entry": "plugin.py"}),
        encoding="utf-8",
    )
    (good / "plugin.py").write_text(
        "from langchain_core.tools import tool\n"
        "from focus.plugins.schemas import PluginDeclaration\n"
        "@tool\ndef good_tool(x: str) -> str:\n    '''good。'''\n    return x\n"
        "def build_plugin(context):\n    return PluginDeclaration(tools=[good_tool])\n",
        encoding="utf-8",
    )
    bad = root / "bad-remote"
    bad.mkdir(parents=True)
    (bad / "plugin.json").write_text(
        json.dumps({
            "name": "bad-remote", "version": "1", "provides": ["tool"],
            "runtime": {"command": "definitely-not-a-real-command-xyz", "args": []},
        }), encoding="utf-8",
    )
    registry = PluginRegistry(builtin_catalog())
    from focus.plugins.loader import load_plugins

    load_plugins(registry, root)
    records = {item["name"]: item for item in registry.list_plugins()}
    assert records["bad-remote"]["status"] == "unavailable"
    assert "插件自身运行环境不可用" in records["bad-remote"]["reason"]
    assert records["good-plugin"]["status"] == "active"
    assert [tool_.name for tool_ in registry.tools()] == ["good_tool"]


def test_remote_process_exit_fails_calls_only_for_that_plugin(tmp_path):
    registry = _remote_plugin(tmp_path, _python_runtime(tmp_path))

    async def run():
        tool_ = registry.tools()[0]
        assert await tool_.ainvoke({"text": "before"}) == "remote:before"
        # 找到远程进程并终止
        from focus.plugins.remote import _remote_handles

        remote = _remote_handles[-1]
        remote.close()
        import time

        deadline = time.monotonic() + 5
        while remote._proc.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        from langchain_core.tools import ToolException

        with pytest.raises(ToolException, match="插件进程已退出"):
            await tool_.ainvoke({"text": "after"})

    asyncio.run(run())


def test_remote_and_inprocess_hooks_interleave_in_stable_order(tmp_path):
    registry = _remote_plugin(tmp_path, _python_runtime(tmp_path))
    registry.register(
        _manifest("inproc", provides=["hook.before_model"]),
        PluginDeclaration(hooks={"hook.before_model": [
            lambda state, runtime: {"messages": state["messages"] + ["inproc"]},
        ]}),
    )
    registry.resolve_dependencies()

    async def run():
        bridge = PluginBridgeMiddleware(registry)
        diff = await bridge.abefore_model({"messages": ["origin"]}, None)
        # 加载顺序：远程插件先加载、进程内插件后登记 → 远程先执行
        assert diff["messages"] == ["origin", {"role": "human", "content": "remote-added"}, "inproc"]
        traces = registry.traces()
        assert [item["plugin"] for item in traces] == ["inproc", "remote-p"]

    asyncio.run(run())


def test_make_lead_agent_injects_remote_tool(tmp_path, monkeypatch):
    registry = _remote_plugin(tmp_path, _python_runtime(tmp_path))
    monkeypatch.setattr("focus.agents.lead.agent.get_plugin_registry", lambda: registry)

    async def run():
        from focus.agents.lead import make_lead_agent

        agent = await make_lead_agent(
            model_name=None, tools=[], system_prompt="", middlewares=[],
            app_config=_fake_app_config(),
        )
        assert "remote_echo" in agent.nodes["tools"].bound.tools_by_name

    asyncio.run(run())


_NODE_RUNTIME = r"""
import readline from "node:readline";
const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const msg = JSON.parse(line);
  if (msg.type === "hello") {
    console.log(JSON.stringify({ type: "hello", id: msg.id, tools: [{
      name: "node_echo", description: "node echo tool",
      schema: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
    }], hooks: [] }));
  } else if (msg.type === "tool_call") {
    console.log(JSON.stringify({ type: "tool_result", id: msg.id, content: "node:" + (msg.args && msg.args.text) }));
  }
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="宿主机未安装 node，跳过语言中立性验证")
def test_remote_plugin_in_node_runtime(tmp_path):
    root = tmp_path / "plugins"
    plugin_dir = root / "node-p"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({
            "name": "node-p", "version": "1.0.0", "enabled": True,
            "provides": ["tool"],
            "runtime": {"command": "node", "args": ["plugin.mjs"]},
        }), encoding="utf-8",
    )
    (plugin_dir / "plugin.mjs").write_text(_NODE_RUNTIME, encoding="utf-8")
    registry = PluginRegistry(builtin_catalog())
    from focus.plugins.loader import load_plugins

    load_plugins(registry, root)
    records = {item["name"]: item for item in registry.list_plugins()}
    assert records["node-p"]["status"] == "active"
    assert [tool_.name for tool_ in registry.tools()] == ["node_echo"]

    async def run():
        assert await registry.tools()[0].ainvoke({"text": "hi"}) == "node:hi"

    asyncio.run(run())


# === 4.1 调试查询接口 ===


def test_plugins_routes_serve_registry_data(monkeypatch):
    registry = PluginRegistry(builtin_catalog())
    registry.register(_manifest("p1", provides=["tool"]), PluginDeclaration(tools=[plugin_browser]))
    registry.resolve_dependencies()
    registry.trace("hook.before_model", "p1", "success", 1.0)
    monkeypatch.setattr("backend.app.desktop.plugins_routes.get_plugin_registry", lambda: registry)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.desktop.plugins_routes import plugins_router

    app = FastAPI()
    app.include_router(plugins_router)
    client = TestClient(app)

    data = client.get("/desktop/api/plugins").json()
    assert data["plugins"][0]["name"] == "p1"
    assert data["plugins"][0]["status"] == "active"
    assert data["interfaces"]["tool"]["plugins"] == ["p1"]
    traces = client.get("/desktop/api/plugins/traces").json()["traces"]
    assert traces[0]["plugin"] == "p1"
    assert traces[0]["status"] == "success"


def _fake_app_config():
    from focus.config.app_config import AppConfig

    return AppConfig(
        models=[{
            "name": "deepseek-v4-flash", "display_name": "deepseek-v4-flash",
            "use": "focus.models.deepseek:DeepSeekChatOpenAI",
            "model": "deepseek-v4-flash", "context_window": 131072,
            "api_key": "sk-test", "base_url": "https://api.deepseek.com",
            "default": True,
        }],
    )


# === f18 路由与前端资源挂载 ===


def _asset_plugin(root: Path, name: str, *, routes: bool = True, assets: bool = True,
                  enabled: bool = True, extra_manifest: dict | None = None) -> Path:
    plugin_dir = root / "plugins" / name
    plugin_dir.mkdir(parents=True)
    manifest = {
        "name": name, "version": "1.0.0", "enabled": enabled,
        "provides": ["tool"], "entry": "plugin.py",
    }
    manifest.update(extra_manifest or {})
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_dir / "plugin.py").write_text(
        "from langchain_core.tools import tool\n"
        "from focus.plugins.schemas import PluginDeclaration\n"
        f"@tool\ndef tool_{name.replace('-', '_')}(x: str) -> str:\n"
        f"    '''{name} 工具。'''\n    return x\n"
        "def build_plugin(context):\n"
        f"    return PluginDeclaration(tools=[tool_{name.replace('-', '_')}])\n",
        encoding="utf-8",
    )
    if routes:
        (plugin_dir / "routes.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/ping')\n"
            "async def ping():\n    return {'pong': True}\n",
            encoding="utf-8",
        )
        manifest["http_routes"] = "routes.py"
    if assets:
        assets_dir = plugin_dir / "desktop"
        assets_dir.mkdir()
        (assets_dir / "entry.js").write_text("window.testPlugin = true;", encoding="utf-8")
        (assets_dir / "style.css").write_text("body{}", encoding="utf-8")
        manifest["desktop_assets"] = "desktop"
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return plugin_dir


def _load(root: Path) -> PluginRegistry:
    registry = PluginRegistry(builtin_catalog())
    from focus.plugins.loader import load_plugins

    load_plugins(registry, root / "plugins")
    return registry


def test_assets_missing_route_file_marks_unavailable(tmp_path):
    plugin_dir = _asset_plugin(tmp_path, "broken", routes=False)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({
            "name": "broken", "version": "1", "enabled": True,
            "provides": ["tool"], "entry": "plugin.py", "http_routes": "routes.py",
        }), encoding="utf-8",
    )
    registry = _load(tmp_path)
    records = {item["name"]: item for item in registry.list_plugins()}
    assert records["broken"]["status"] == "unavailable"
    assert "路由模块缺失" in records["broken"]["reason"]
    assert registry.tools() == []
    assert registry.active_assets() == {}


def test_assets_missing_assets_dir_marks_unavailable(tmp_path):
    _asset_plugin(tmp_path, "nodir", assets=False)
    (tmp_path / "plugins" / "nodir" / "plugin.json").write_text(
        json.dumps({
            "name": "nodir", "version": "1", "enabled": True,
            "provides": ["tool"], "entry": "plugin.py", "desktop_assets": "desktop",
        }), encoding="utf-8",
    )
    registry = _load(tmp_path)
    records = {item["name"]: item for item in registry.list_plugins()}
    assert records["nodir"]["status"] == "unavailable"
    assert "前端资源目录缺失" in records["nodir"]["reason"]


def test_assets_collected_for_active_plugin(tmp_path):
    _asset_plugin(tmp_path, "spatial", extra_manifest=None)
    registry = _load(tmp_path)
    records = {item["name"]: item for item in registry.list_plugins()}
    assert records["spatial"]["status"] == "active"
    assert records["spatial"]["desktop_assets"] == ["entry.js", "style.css"]
    assets = registry.active_assets()
    assert set(assets) == {"spatial"}
    assert assets["spatial"]["router"] is not None
    assert (assets["spatial"]["assets_dir"] / "entry.js").is_file()


def test_assets_disabled_plugin_not_mounted(tmp_path):
    _asset_plugin(tmp_path, "off", enabled=False)
    registry = _load(tmp_path)
    assert registry.active_assets() == {}
    assert all(item["desktop_assets"] == [] for item in registry.list_plugins())


def test_assets_rejected_plugin_not_mounted(tmp_path):
    _asset_plugin(tmp_path, "bad-interface", extra_manifest={"provides": ["hook.unknown_point"]})
    registry = _load(tmp_path)
    records = {item["name"]: item for item in registry.list_plugins()}
    assert records["bad-interface"]["status"] == "rejected"
    assert registry.active_assets() == {}


def test_assets_empty_plugin_dir_zero_impact(tmp_path):
    registry = _load(tmp_path)
    assert registry.active_assets() == {}
