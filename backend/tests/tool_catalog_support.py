"""本文件对外提供工具目录测试的 registry、离线模型及实际 Desktop 工厂构造入口。

输入为隔离工具、Provider、压缩开关和 pytest monkeypatch；输出为真实执行图、捕获的请求与计数器。
工作流为声明测试插件，保留实际发现和插件桥，只替换耐久 inbox／attempt 存储端口，再驱动真实工厂。
示例：service = desktop_service(config)；graph = await service._build_agent_factory(... )()。
"""

import json

import httpx
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import StructuredTool

import focus.agents.lead.agent as lead
import focus.plugins as plugins
import focus.tools.tools as discovery
import backend.app.desktop.service as desktop
from backend.app.desktop.collab import AgentCollab
from focus.config import AppConfig
from focus.config.commitment_config import CommitmentConfig
from focus.config.compression_config import CompressionConfig
from focus.config.model_config import ModelConfig
from focus.models.provider_contract import ProviderContract
from focus.models.responses import FocusResponsesChatModel
from focus.plugins.interfaces import builtin_catalog
from focus.plugins.registry import PluginRegistry
from focus.plugins.schemas import PluginDeclaration, PluginManifest
from focus.security.effects import NO_LOCAL_EFFECT, declare_effect


class IsolatedInbox(AgentMiddleware):
    pass


class IsolatedAttempt(AgentMiddleware):
    pass


def test_tool(name, calls, *, effect=NO_LOCAL_EFFECT):
    def invoke(text: str) -> str:
        calls.append((name, text))
        return f"{name}:{text}"
    result = StructuredTool.from_function(invoke, name=name, description=f"Test {name}")
    return declare_effect(result, effect)


test_tool.__test__ = False


def registry_for(tools, hooks=None):
    registry = PluginRegistry(builtin_catalog())
    hooks = hooks or {}
    registry.register(PluginManifest(name="fixture", version="1", provides=["tool", *hooks]),
                      PluginDeclaration(tools=tools, hooks=hooks))
    registry.resolve_dependencies()
    return registry


def model_fixture(provider, captured, *, call_tool=None):
    def handle(request):
        payload = json.loads(request.content)
        captured.append(payload)
        output = [{"type": "message", "id": "answer", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": "done", "annotations": []}]}]
        if call_tool and len(captured) == 1:
            output = [{"type": "function_call", "id": "invoke", "call_id": "call-plugin", "status": "completed",
                       "name": call_tool, "arguments": '{"text":"hello"}'}]
        return httpx.Response(200, json={"object": "response", "id": f"response-{len(captured)}",
            "created_at": 0, "model": "fixture", "status": "completed", "output": output})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    model = FocusResponsesChatModel(model="fixture", api_key="unused", base_url="https://fixture.test/v1",
                                   provider_contract=ProviderContract(provider, "responses", context_window=1000000),
                                   http_async_client=client)
    return model, client


def app_config(provider, compression):
    return AppConfig(models=[ModelConfig(name="fixture", display_name="Fixture", default=True,
        provider=provider, protocol="responses", use="focus.models.responses:FocusResponsesChatModel",
        model="fixture", api_key="unused", base_url="https://fixture.test/v1", context_window=1000000)],
        commitment=CommitmentConfig(enabled=False), compression=CompressionConfig(enabled=compression))


def desktop_service(config):
    service = object.__new__(desktop.DesktopService)
    service.app_config = config
    service.agent_collab = AgentCollab(None)
    service.session_factory = None
    service.checkpointer = None
    return service


def install_fixture(monkeypatch, registry, model, *, custom=(), mcp=()):
    async def custom_tools():
        return [discovery._wrap_as_toolinfo(item, "custom") for item in custom]
    async def mcp_tools():
        return [discovery._wrap_as_toolinfo(item, "mcp") for item in mcp]
    monkeypatch.setattr(plugins, "get_plugin_registry", lambda: registry)
    monkeypatch.setattr(lead, "get_plugin_registry", lambda: registry)
    monkeypatch.setattr(lead, "create_chat_model", lambda **kwargs: model)
    monkeypatch.setattr(discovery, "_custom_tools", custom_tools)
    monkeypatch.setattr(discovery, "_mcp_tools", mcp_tools)
    monkeypatch.setattr(desktop, "DurableInboxMiddleware", lambda *args: IsolatedInbox())
    monkeypatch.setattr(desktop, "ModelAttemptMiddleware", lambda *args: IsolatedAttempt())
