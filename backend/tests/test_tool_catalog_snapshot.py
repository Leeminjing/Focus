"""本文件对外提供插件 reload、容器快照与只读工具执行的集成验证。

输入为隔离插件文件、真实 reload 与执行图；输出为新旧绑定隔离、原 metadata 和权限拒绝断言。
工作流为构图后更换插件并重载，比较旧图／新图，再在只读上下文调用已公布的写工具。
示例：pytest backend/tests/test_tool_catalog_snapshot.py；使用临时目录与离线 Provider，无生产数据。
"""

import asyncio
import json

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain.tools import ToolRuntime
from langgraph.checkpoint.memory import InMemorySaver

import focus.agents.lead.agent as lead
from focus.plugins import reload_plugins
from focus.plugins.bridge import PluginBridgeMiddleware
from focus.security.effects import NO_LOCAL_EFFECT, ResolvedFsEffect, declare_effect, effect_of, structured_fs
from focus.security.policy import AccessMode
from backend.tests.runtime_context_support import runtime_context
from backend.tests.tool_catalog_support import install_fixture, model_fixture, registry_for, test_tool


def _write_plugin(root, name, enabled=True):
    directory = root / "fixture"
    directory.mkdir(exist_ok=True)
    (directory / "plugin.json").write_text(json.dumps({"name": "fixture", "version": "1", "enabled": enabled,
        "provides": ["tool"], "entry": "plugin.py"}), encoding="utf-8")
    source = '\n'.join([
        '"""本文件对外提供 build_plugin；输入为插件上下文，输出为测试工具声明；流程为构造单工具；示例：build_plugin(context)。"""',
        'from langchain_core.tools import StructuredTool',
        'from focus.plugins.schemas import PluginDeclaration',
        'def echo(text: str) -> str:',
        f'    return "{name}:" + text',
        'def build_plugin(context):',
        f'    return PluginDeclaration(tools=[StructuredTool.from_function(echo, name="{name}", description="Reload probe")])',
    ])
    (directory / "plugin.py").write_text(source, encoding="utf-8")


def test_real_reload_and_disabling_affect_only_new_graphs(monkeypatch, tmp_path):
    async def run():
        root = tmp_path / "plugins"
        root.mkdir()
        _write_plugin(root, "before_reload")
        original = reload_plugins(root)
        captured = []
        model, client = model_fixture("openai", captured)
        install_fixture(monkeypatch, original, model)
        try:
            old_graph = await lead.make_lead_agent(model_name="fixture", tools=[], system_prompt="base", middlewares=[])
            old_tools = old_graph.get_graph().nodes["tools"].data.tools_by_name
            original_tool = old_tools["before_reload"]
            _write_plugin(root, "after_reload_with_changed_length")
            latest = reload_plugins(root)
            install_fixture(monkeypatch, latest, model)
            new_graph = await lead.make_lead_agent(model_name="fixture", tools=[], system_prompt="base", middlewares=[])
            new_tools = new_graph.get_graph().nodes["tools"].data.tools_by_name
            assert list(old_tools) == ["before_reload"] and old_tools["before_reload"] is original_tool
            assert list(new_tools) == ["after_reload_with_changed_length"]
            assert original_tool.invoke({"text": "x"}) == "before_reload:x"
            assert new_tools["after_reload_with_changed_length"].invoke({"text": "x"}) == "after_reload_with_changed_length:x"
            _write_plugin(root, "after_reload_with_changed_length", enabled=False)
            disabled = reload_plugins(root)
            install_fixture(monkeypatch, disabled, model)
            disabled_graph = await lead.make_lead_agent(model_name="fixture", tools=[], system_prompt="base", middlewares=[])
            assert not disabled_graph.get_graph().nodes["tools"].data.tools_by_name
            assert original.tools() == [original_tool] and len(new_tools) == 1
            disabled_graph.checkpointer = InMemorySaver()
            values = await disabled_graph.ainvoke({"messages": [HumanMessage(content="hello")]},
                {"configurable": {"thread_id": "disabled"}}, context=runtime_context(
                    agent_id="main", task_id="task", workspace=str(tmp_path)))
            assert captured[0]["tools"] == []
            assert values["world_state_snapshot"]["sections"]["tool_catalog"]["data"] == {}
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())


def test_plugin_runtime_argument_remains_injected_and_hidden_from_model(monkeypatch, tmp_path):
    async def run():
        captured, calls = [], []
        def invoke(text: str, runtime: ToolRuntime[dict]) -> str:
            calls.append((text, runtime.context["agent_id"], runtime.tool_call_id))
            return text
        plugin = declare_effect(StructuredTool.from_function(invoke, name="runtime_probe", description="Runtime injection"),
                                NO_LOCAL_EFFECT)
        model, client = model_fixture("openai", captured, call_tool="runtime_probe")
        install_fixture(monkeypatch, registry_for([plugin]), model)
        try:
            graph = await lead.make_lead_agent(model_name="fixture", tools=[], system_prompt="base", middlewares=[])
            graph.checkpointer = InMemorySaver()
            await graph.ainvoke({"messages": [HumanMessage(content="hello")]},
                {"configurable": {"thread_id": "runtime"}}, context=runtime_context(
                    agent_id="main", task_id="task", workspace=str(tmp_path)))
            assert calls == [("hello", "main", "call-plugin")]
            spec = next(tool for tool in captured[0]["tools"] if tool["name"] == "runtime_probe")
            assert "runtime" not in spec["parameters"]["properties"]
            assert graph.get_graph().nodes["tools"].data.tools_by_name["runtime_probe"] is plugin
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())


def test_bridge_container_isolated_from_consumers_and_registry_growth():
    first, second = test_tool("first", []), test_tool("second", [])
    registry = registry_for([first])
    bridge = PluginBridgeMiddleware(registry)
    exposed = bridge.tools
    exposed.clear()
    registry._tools.append(("later", second))
    assert bridge.tools == [first] and bridge.tools[0] is first
    assert PluginBridgeMiddleware(registry).tools == [first, second]


def test_advertised_write_tool_retains_effect_and_is_denied_in_read_only_run(monkeypatch, tmp_path):
    async def run():
        captured, calls = [], []
        effect = structured_fs(lambda args, context: ResolvedFsEffect(writes=(tmp_path / "output.txt",)))
        write = test_tool("write_probe", calls, effect=effect)
        registry = registry_for([write])
        model, client = model_fixture("openai", captured, call_tool="write_probe")
        install_fixture(monkeypatch, registry, model)
        try:
            graph = await lead.make_lead_agent(model_name="fixture", tools=[], system_prompt="base", middlewares=[])
            bound = graph.get_graph().nodes["tools"].data.tools_by_name["write_probe"]
            assert bound is write and effect_of(bound) is effect
            graph.checkpointer = InMemorySaver()
            result = await graph.ainvoke({"messages": [HumanMessage(content="write please")]},
                {"configurable": {"thread_id": "read-only"}}, context=runtime_context(agent_id="main", task_id="task",
                    workspace=str(tmp_path), access_mode=AccessMode.READ_ONLY, permissions=("read", "write")))
            assert not calls and not (tmp_path / "output.txt").exists()
            assert any(tool["name"] == "write_probe" for tool in captured[0]["tools"])
            output = next(message for message in result["messages"] if isinstance(message, ToolMessage))
            assert output.status == "error" and "read-only" in str(output.content)
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())
