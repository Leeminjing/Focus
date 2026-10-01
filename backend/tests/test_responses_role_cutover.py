"""本文件对外提供角色工厂、完整预算与维护准入的 Responses 验证。

输入为离线 HTTP 终态、明确模型配置和受治理角色上下文；输出为实际请求协议、来源与恢复隔离断言。
具体工作流为调用真实模型/Agent 工厂，检查 Main、Teammate、Worker 和冻结认知请求，再比较压缩门与 SDK 请求预算。
旧 v5 索引以独立黄金哈希保持可读且不改写；当前 v8 索引使用新身份并可通过持久化校验重新加载。
示例：pytest backend/tests/test_responses_role_cutover.py；不使用外部模型或用户数据库。
"""

import asyncio
import json
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from backend.app.desktop.agent_loop.structured_worker import StructuredWorkerModel
from backend.app.desktop.service import DesktopService
from focus.agents.compression.gate import CompressionGate
from focus.agents.lead import agent as lead
from focus.config import AppConfig
from focus.config.model_config import ModelConfig
from focus.context.middleware import WorldStateMiddleware
from focus.context.requests import frozen_request_messages
from focus.history import FocusItem, HistoryPayload, history_records, items_to_messages, messages_to_items, semantic_messages
from focus.history.bridge import branch_messages
from focus.messages.request_budget import estimate_responses_budget
from focus.models.factory import create_chat_model
from focus.security.context import AuthorizationIdentity, RoutingIdentity, SecurityContext
from focus.security.policy import AccessMode


def _config(provider):
    return AppConfig(models=[ModelConfig(name="fixture", display_name="Fixture",
        use="focus.models.responses:FocusResponsesChatModel", provider=provider, protocol="responses",
        model="fixture", api_key="fixture", base_url="https://fixture.test/v1", default=True,
        curation_output_method="json_schema", context_window=100000)])


def _response():
    return {"object": "response", "id": "fixture", "created_at": 0, "model": "fixture", "status": "completed",
        "output": [{"type": "message", "id": "m", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": '{"ok":true}', "annotations": []}]}]}


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize("role", ["main", "teammate", "worker"])
def test_actual_agent_factory_protocol_and_context_sources(provider, role, tmp_path, monkeypatch):
    async def run():
        requests = []
        def handle(request):
            assert request.url.path == "/v1/responses"
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=_response())
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        config = _config(provider)
        model = create_chat_model("fixture", app_config=config, http_async_client=client)
        monkeypatch.setattr(lead, "create_chat_model", lambda **_: model)
        graph = await lead.make_lead_agent(model_name="fixture", agent_name=role, tools=[],
            system_prompt="base:" + role, middlewares=[], app_config=config)
        graph.checkpointer = InMemorySaver()
        security = SecurityContext(AuthorizationIdentity(tmp_path, (tmp_path,), ("read",), AccessMode.READ_ONLY, role),
                                   RoutingIdentity(role, "workspace", role, "context", "", "run"))
        try:
            state = await graph.ainvoke({"messages": [HumanMessage(content="task")]},
                                        {"configurable": {"thread_id": role}},
                                        context=security.to_runtime_context(), durability="sync")
            payload = requests[0]
            assert payload["instructions"] == "base:" + role
            assert "messages" not in payload and "previous_response_id" not in payload
            assert payload.get("store") is False if provider == "openai" else "store" not in payload
            policy_role = "developer" if provider == "openai" else "system"
            assert any(item.get("role") == policy_role and "permissions" in str(item) for item in payload["input"])
            assert state["request_manifest"]["binding"]["provider_contract"]["provider"] == provider
            metadata = state["messages"][-1].response_metadata
            assert metadata["request_source_manifest"]["instructions_hash"]
            assert metadata.get("usage") is None
            assert len(semantic_messages([FocusItem.model_validate(item)
                                          for item in state["execution_items"]])) == 2
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_actual_structured_worker_has_frozen_sources(provider, monkeypatch):
    async def run():
        from backend.app.desktop.agent_loop import structured_worker
        requests = []
        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=_response())
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        config = _config(provider)
        model = create_chat_model("fixture", app_config=config, http_async_client=client)
        monkeypatch.setattr(structured_worker, "create_chat_model", lambda **_: model)
        class Result(BaseModel):
            ok: bool
        worker = StructuredWorkerModel(config)
        try:
            assert (await worker.invoke(Result, "cognitive policy", {"observation_id": "frozen-round"})).ok
            assert requests[0]["text"]["format"]["type"] == "json_schema"
            source = worker.last_model_metadata["request_source_manifest"]
            assert source["inputs"][1]["context"]["scope"] == "round"
            assert source["inputs"][1]["context"]["source_refs"][0]["payload_hash"]
            assert not worker.last_usage_reported
        finally:
            await client.aclose()
            model._client.close()
    asyncio.run(run())


def test_pending_world_state_budget_is_identical_to_actual_request(tmp_path):
    config = _config("deepseek")
    model = create_chat_model("fixture", app_config=config, text={"format": {"type": "json_object"}})
    context = SecurityContext(AuthorizationIdentity(tmp_path, (tmp_path,), ("read",), AccessMode.READ_ONLY, "main"),
        RoutingIdentity("t", "ws", "main", "c", "", "r")).to_runtime_context()
    world = WorldStateMiddleware([], "base", frozenset(), "fixture", model.provider_contract.projection_version)
    state = {"messages": [HumanMessage(content="task", id="user")]}
    gate = CompressionGate(100000, .9)
    gate.configure_request("base", [], model=model, world_state=world)
    try:
        messages = [SystemMessage(content="base"), *world.preview_messages(state, context)]
        actual = model.request_payload(messages)
        assert gate._request_usage(state, context, ()) == estimate_responses_budget(actual)
        assert gate._request_usage(state, context, ()) > estimate_responses_budget(model.request_payload(state["messages"]))
    finally:
        model._client.close()


def test_maintenance_admission_preserves_v2_read_and_explicit_new_branch(tmp_path):
    from fastapi import HTTPException
    service = object.__new__(DesktopService)
    service.app_config = SimpleNamespace(context_run_admission=False)
    with pytest.raises(HTTPException) as caught:
        service._require_run_admission()
    assert caught.value.status_code == 503
    policy = frozen_request_messages("base", "old run selection")[1]
    task = HumanMessage(content="task", id="task")
    items = messages_to_items([task, policy])
    history = HistoryPayload(execution_items=items)
    assert history_records(history.execution_items)[0]["content"] == "task"
    assert [message.content for message in branch_messages(items_to_messages(items))] == ["task"]


def test_independent_head_v5_golden_preserves_hash_and_v8_is_a_new_identity():
    from backend.app.desktop.agent_loop.context_expansion.semantic_index import RevisionSemanticIndex
    from backend.app.desktop.agent_loop.context_expansion.semantic_indexer import RevisionSemanticIndexer
    path = Path(__file__).parent / "fixtures/responses/legacy-v5-index.json"
    before = path.read_bytes()
    old = RevisionSemanticIndex.model_validate_json(before)
    assert old.index_schema_version == "revision-semantic-index-v5"
    assert old.index_id == "7be7ed7fe4a2c418a0109276037031e05fb7aebbb76e1da66f79788aee554606"
    current = RevisionSemanticIndexer().index(source=old.source, source_content_hash=old.source_content_hash,
        context_role=old.context_role, active_objective=old.active_objective,
        raw_messages=[{"id": message.message_id, "role": message.role, "content": message.content} for message in old.messages])
    assert current.index_schema_version == "revision-semantic-index-v8"
    assert current.index_id != old.index_id
    assert current.source == old.source and current.source_content_hash == old.source_content_hash
    assert RevisionSemanticIndex.model_validate_json(current.model_dump_json()) == current
    assert path.read_bytes() == before


def test_model_config_does_not_construct_unused_sdk_clients(monkeypatch):
    from focus.models import response_clients as module
    constructions = []
    def unexpected(**kwargs):
        constructions.append(True)
        raise AssertionError("配置解析不得初始化 SDK/TLS")
    monkeypatch.setattr(module, "OpenAI", unexpected)
    monkeypatch.setattr(module, "AsyncOpenAI", unexpected)
    model = create_chat_model("fixture", app_config=_config("deepseek"))
    model.bind_tools([])
    model.with_structured_output({"title": "UnusedContract", "type": "object", "properties": {}})
    model._client.close()
    asyncio.run(model._async_client.close())
    assert constructions == []
