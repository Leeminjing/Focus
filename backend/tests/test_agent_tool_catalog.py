"""本文件对外提供实际工具装配与执行的一致性回归。

输入为真实 Desktop／Harness 工厂、非空隔离插件池与离线 Responses；输出为唯一目录、路由和副作用断言。
工作流为保留两条生产注入路径，构图后驱动模型与工具交换，并检查能力上下文和权限未被目录改变。
插件与后置 attempt 工具分别完成 OpenAI／DeepSeek 的实际离线调用，直接核对 wire 目录、原 callable 和完整预算。
示例：pytest backend/tests/test_agent_tool_catalog.py；不访问真实 API 或用户数据库。
"""

import asyncio

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from backend.tests.runtime_context_support import runtime_context
from backend.tests.tool_catalog_support import app_config, desktop_service, install_fixture, model_fixture, registry_for, test_tool
import focus.agents.lead.agent as lead
from focus.agents.compression.gate import CompressionGate
from focus.context.middleware import WorldStateMiddleware
from focus.history import content_hash
from focus.messages.request_budget import estimate_responses_budget
from langchain_core.utils.function_calling import convert_to_openai_tool
from focus.models.response_projection import function_specs
from focus.tools.catalog import ToolNameConflict


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize("compression", [True, False])
def test_actual_desktop_main_plugin_and_compression(monkeypatch, tmp_path, provider, compression):
    async def run():
        captured, calls = [], []
        registry = registry_for([test_tool("demo_echo", calls)])
        model, client = model_fixture(provider, captured, call_tool="demo_echo")
        install_fixture(monkeypatch, registry, model)
        service = desktop_service(app_config(provider, compression))
        assembled = []
        create = lead.create_agent
        def capture(**kwargs):
            assembled.append(kwargs)
            return create(**kwargs)
        monkeypatch.setattr(lead, "create_agent", capture)
        try:
            factory = service._build_agent_factory("task", "main", str(tmp_path),
                {"permissions": ["read"], "model_name": "fixture"}, "base", "", "main")
            try:
                graph = await factory()
            except ValueError:
                assert not captured and not calls
                raise
            assert not captured and not calls
            graph.checkpointer = InMemorySaver()
            result = await graph.ainvoke({"messages": [HumanMessage(content="你好")]},
                {"configurable": {"thread_id": "fixture"}},
                context=runtime_context(agent_id="main", task_id="task", workspace=str(tmp_path)))
            assert len(captured) == 2 and calls == [("demo_echo", "hello")]
            names = [spec["name"] for spec in captured[0]["tools"]]
            assert names.count("demo_echo") == 1
            assert result["messages"][-1].content == "done"
            node = graph.get_graph().nodes["tools"].data
            assert list(node.tools_by_name) == names
            assert node.tools_by_name["demo_echo"] is registry.tools()[0]
            specs = [convert_to_openai_tool(tool) for tool in node.tools_by_name.values()]
            world = next(item for item in assembled[0]["middleware"] if isinstance(item, WorldStateMiddleware))
            assert world._tool_specs == specs
            assert function_specs(specs) == captured[0]["tools"]
            section = result["world_state_snapshot"]["sections"]["tool_catalog"]["data"]
            assert set(section) == set(names)
            assert all(section[spec["function"]["name"]]["schema_hash"] == content_hash(spec) for spec in specs)
            if compression:
                gate = next(item for item in assembled[0]["middleware"] if isinstance(item, CompressionGate))
                initial = {"messages": [HumanMessage(content="你好")]}
                context = runtime_context(agent_id="main", task_id="task", workspace=str(tmp_path))
                assert gate._request_usage(initial, context, ()) == estimate_responses_budget(captured[0])
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())


@pytest.mark.parametrize("role", ["main", "teammate", "worker", "patrol"])
def test_role_factory_preserves_ordinary_tools_and_plugin_hooks(monkeypatch, tmp_path, role):
    async def run():
        captured, hooks = [], []
        async def before(state, runtime):
            hooks.append("before")
        registry = registry_for([test_tool("plugin_probe", [])], {"hook.before_model": [before]})
        custom, mcp = test_tool("custom_probe", []), test_tool("mcp_probe", [])
        model, client = model_fixture("openai", captured)
        install_fixture(monkeypatch, registry, model, custom=[custom], mcp=[mcp])
        try:
            service = desktop_service(app_config("openai", True))
            graph = await service._build_agent_factory("task", role, str(tmp_path),
                {"permissions": ["read"], "model_name": "fixture"}, "base", "", role)()
            graph.checkpointer = InMemorySaver()
            await graph.ainvoke({"messages": [HumanMessage(content="hi")]},
                {"configurable": {"thread_id": role}},
                context=runtime_context(agent_id=role, task_id="task", workspace=str(tmp_path), agent_role=role))
            names = [tool["name"] for tool in captured[0]["tools"]]
            assert len(names) == len(set(names)) and names.count("plugin_probe") == 1
            assert ("custom_probe" in names) == (role == "main")
            assert ("mcp_probe" in names) == (role == "main")
            assert ("web_search" in names) == (role != "patrol")
            assert ("send_message" in names) == (role != "patrol")
            assert "read_file" in names and hooks == ["before"]
            equipment = await service._equipment_tools()
            assert any(row["name"] == "plugin_probe" and row["source"] == "plugin" for row in equipment)
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())


def test_default_harness_factory_keeps_non_plugin_discovery(monkeypatch, tmp_path):
    async def run():
        captured = []
        registry = registry_for([test_tool("plugin_probe", [])])
        model, client = model_fixture("openai", captured)
        install_fixture(monkeypatch, registry, model, custom=[test_tool("custom_probe", [])], mcp=[test_tool("mcp_probe", [])])
        try:
            graph = await lead.make_lead_agent(model_name="fixture", tools=None, system_prompt="base", middlewares=[],
                                               app_config=app_config("openai", False))
            node = graph.get_graph().nodes["tools"].data
            assert all(name in node.tools_by_name for name in ["plugin_probe", "custom_probe", "mcp_probe", "read_file", "describe_skill"])
            assert sum(name == "plugin_probe" for name in node.tools_by_name) == 1
            assert not captured
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())


@pytest.mark.parametrize("compression", [True, False])
@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_real_factory_conflicts_fail_before_http_or_tool_effects(monkeypatch, compression, protocol):
    async def run():
        from langchain_openai import ChatOpenAI
        captured, calls = [], []
        registry = registry_for([test_tool("collision", calls)])
        model, client = model_fixture("openai", captured)
        install_fixture(monkeypatch, registry, model)
        if protocol == "chat_completions":
            monkeypatch.setattr(lead, "create_chat_model", lambda **kwargs: ChatOpenAI(
                model="fixture", api_key="unused", base_url="https://fixture.invalid/v1"))
        try:
            with pytest.raises(ToolNameConflict) as caught:
                await lead.make_lead_agent(model_name="fixture", tools=[test_tool("collision", calls)],
                    system_prompt="base", middlewares=[], additional_middlewares=[CompressionGate(1000000, .9)] if compression else [])
            assert "PluginBridgeMiddleware" in str(caught.value) and "tools[0]" in str(caught.value)
            assert not captured and not calls
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())


def test_packaged_demo_plugin_is_injected_once(monkeypatch, tmp_path):
    async def run():
        from pathlib import Path
        from focus.plugins.loader import load_plugins
        from focus.plugins.interfaces import builtin_catalog
        from focus.plugins.registry import PluginRegistry
        registry = PluginRegistry(builtin_catalog())
        load_plugins(registry, Path(__file__).resolve().parents[2] / "plugins")
        assert any(tool.name == "demo_echo" for tool in registry.tools())
        model, client = model_fixture("openai", [])
        install_fixture(monkeypatch, registry, model)
        try:
            service = desktop_service(app_config("openai", True))
            graph = await service._build_agent_factory("task", "main", str(tmp_path),
                {"permissions": ["read"], "model_name": "fixture"}, "base", "", "main")()
            names = list(graph.get_graph().nodes["tools"].data.tools_by_name)
            assert names.count("demo_echo") == 1
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_late_middleware_tool_is_visible_everywhere(monkeypatch, tmp_path, provider):
    async def run():
        requests, calls = [], []
        model, client = model_fixture(provider, requests, call_tool="attempt_probe")
        registry = registry_for([test_tool("plugin_probe", [])])
        install_fixture(monkeypatch, registry, model)
        attempt_tool = test_tool("attempt_probe", calls)
        class AttemptTools(AgentMiddleware):
            tools = [attempt_tool]
        captured = []
        create = lead.create_agent
        def capture(**kwargs):
            captured.append(kwargs)
            return create(**kwargs)
        monkeypatch.setattr(lead, "create_agent", capture)
        try:
            gate = CompressionGate(1000000, .9)
            graph = await lead.make_lead_agent(model_name="fixture", tools=[test_tool("ordinary_probe", [])],
                system_prompt="base", middlewares=[], additional_middlewares=[gate], attempt_middleware=AttemptTools())
            names = list(graph.get_graph().nodes["tools"].data.tools_by_name)
            assert names == ["plugin_probe", "attempt_probe", "ordinary_probe"]
            assert [spec["name"] for spec in gate._tools] == names
            world = next(item for item in captured[0]["middleware"] if isinstance(item, WorldStateMiddleware))
            assert [spec["function"]["name"] for spec in world._tool_specs] == names
            assert not requests and not calls
            graph.checkpointer = InMemorySaver()
            context = runtime_context(agent_id="main", task_id="task", workspace=str(tmp_path))
            initial = {"messages": [HumanMessage(content="hello")]}
            result = await graph.ainvoke(initial, {"configurable": {"thread_id": "late-tools"}}, context=context)
            assert len(requests) == 2 and calls == [("attempt_probe", "hello")]
            assert result["messages"][-1].content == "done"
            node = graph.get_graph().nodes["tools"].data
            assert node.tools_by_name["attempt_probe"] is attempt_tool
            specs = [convert_to_openai_tool(tool) for tool in node.tools_by_name.values()]
            assert world._tool_specs == specs
            for payload in requests:
                assert [spec["name"] for spec in payload["tools"]] == names
                assert payload["tools"] == function_specs(specs) == list(gate._tools)
            assert gate._request_usage(initial, context, ()) == estimate_responses_budget(requests[0])
            section = result["world_state_snapshot"]["sections"]["tool_catalog"]["data"]
            assert set(section) == set(names)
            assert all(section[spec["function"]["name"]]["schema_hash"] == content_hash(spec) for spec in specs)
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())
