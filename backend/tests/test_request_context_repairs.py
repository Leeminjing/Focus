"""本文件对外提供压缩恢复与 Responses 前缀重建的回归验证。

输入为真实 Lead 工厂、隔离 SDK transport、冻结上下文及耐久内存 checkpoint；输出为首次请求、重建来源和旧 checkpoint 不变断言。
具体工作流为暂停压缩后恢复，或在两个 Run 间改变图片／选中引用，核对实际 wire 与 typed authority。
示例：pytest backend/tests/test_request_context_repairs.py；不访问真实 Provider。
"""

import asyncio
from dataclasses import replace
import json
import os
import uuid

import httpx
import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

import focus.agents.lead.agent as lead
from focus.agents.compression.gate import CompressionGate
from focus.agents.image_attachment import ImageAttachmentProjectionMiddleware
from focus.context.scoped import FrozenContext, prepare_scoped_contexts
from focus.history.bridge import ExecutionHistoryRebuild, replace_execution_items, synchronize_items
from focus.history import content_hash
from focus.models.provider_contract import ProviderContract
from focus.models.response_continuation import ContinuationMismatch
from focus.models.responses import FocusResponsesChatModel
from focus.security.context import AuthorizationIdentity, RoutingIdentity, SecurityContext
from focus.security.policy import AccessMode


def _model(captured):
    def handle(request):
        payload = json.loads(request.content)
        captured.append(payload)
        identity = str(len(captured))
        return httpx.Response(200, json={"object": "response", "id": "r" + identity, "created_at": 0,
            "model": "fixture", "status": "completed", "error": None, "incomplete_details": None,
            "output": [{"type": "reasoning", "id": "reason" + identity, "summary": [], "encrypted_content": "opaque"},
                       {"type": "message", "id": "answer" + identity, "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "done", "annotations": []}]}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return FocusResponsesChatModel(model="fixture", api_key="fixture", base_url="https://fixture.test/v1",
        provider_contract=ProviderContract("openai", "responses", supports_image_input=True), http_async_client=client), client


def _security(path):
    return SecurityContext(AuthorizationIdentity(path, (path,), (), AccessMode.READ_ONLY, "main"),
                           RoutingIdentity("repair", "workspace", "main", "repair", "", "first"))


def test_compression_resume_restores_frozen_context_before_first_wire(monkeypatch, tmp_path):
    async def run():
        captured = []
        model, client = _model(captured)
        monkeypatch.setattr(lead, "create_chat_model", lambda **kwargs: model)
        contexts = (FrozenContext("memory", "CURRENT_MEMORY"), FrozenContext("skill", "CURRENT_SKILL"),
                    FrozenContext("material_policy", "CURRENT_MATERIAL_POLICY", authority="policy"))
        graph = await lead.make_lead_agent(model_name="fixture", tools=[], system_prompt="base",
            middlewares=[CompressionGate(context_window=2, threshold_ratio=0.5)], frozen_contexts=contexts)
        graph.checkpointer = InMemorySaver()
        config = {"configurable": {"thread_id": "compression-repair", "run_id": "first"}}
        context = _security(tmp_path).to_runtime_context()
        retained = [HumanMessage(id="old", content="old history " * 30), HumanMessage(id="current", content="current task")]
        retained.extend(prepare_scoped_contexts(retained, contexts, "first"))
        initial = await graph.ainvoke({"messages": retained}, config, context=context, durability="sync")
        assert initial.get("__interrupt__") and not captured
        assert all(any(message.content == frozen.content for message in initial["messages"]) for frozen in contexts)
        state = await graph.ainvoke(Command(resume={"type": "compression", "decision": "apply", "ranges": [
            {"source_ids": ["old"], "replacement": "old summary"}]}), config, context=context, durability="sync")
        assert len(captured) == 1
        for frozen in contexts:
            assert sum(item.get("content") == frozen.content for item in captured[0]["input"]) == 1
            assert sum(message.content == frozen.content for message in state["messages"]) == 1
        assert state["request_manifest"]["status"] == "sampled"
        assert len(state["request_manifest"]["selected_bindings"]) == 2
        synchronize_items(state["execution_items"], state["messages"])
        await client.aclose()
        model._client.close()
    asyncio.run(run())


@pytest.mark.usefixtures("isolated_postgres_database")
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("change", ["image", "run_scope", "unchanged"])
def test_actual_prefix_change_rebuilds_authority_and_preserves_old_checkpoint(monkeypatch, tmp_path, change, durable):
    async def run():
        if durable:
            async with AsyncPostgresSaver.from_conn_string(os.environ["FOCUS_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")) as saver:
                await exercise(saver)
        else:
            await exercise(InMemorySaver())

    async def exercise(saver):
        captured = []
        model, client = _model(captured)
        monkeypatch.setattr(lead, "create_chat_model", lambda **kwargs: model)
        contexts = (FrozenContext("memory", "selected memory"),) if change == "run_scope" else ()
        crashed = []
        class PreparedFailure(AgentMiddleware):
            async def abefore_model(self, state, runtime):
                if durable and change == "run_scope" and state["request_manifest"].get("history_rebuild_reason") and not crashed:
                    crashed.append(True)
                    raise RuntimeError("rebuild committed before sampling")
        graph = await lead.make_lead_agent(model_name="fixture", tools=[], system_prompt="base",
            middlewares=[ImageAttachmentProjectionMiddleware()], frozen_contexts=contexts, attempt_middleware=PreparedFailure())
        graph.checkpointer = saver
        config = {"configurable": {"thread_id": "prefix-repair:" + uuid.uuid4().hex}}
        security = _security(tmp_path)
        context = {**security.to_runtime_context(), "origin_message_id": "first-input"}
        content = [{"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}] if change == "image" else "task"
        first = await graph.ainvoke({"messages": [HumanMessage(id="first-input", content=content)]}, config,
                                   context=context, durability="sync")
        old = await graph.aget_state(config)
        old_values = old.values
        response = first["messages"][-1]
        with pytest.raises(ContinuationMismatch):
            model.request_payload([SystemMessage(content="different base"), *first["messages"]])
        unproven = response.model_copy(deep=True)
        unproven.response_metadata.pop("request_source_manifest")
        with pytest.raises(ContinuationMismatch):
            model.request_payload([SystemMessage(content="base"), *first["messages"][:-1], unproven])
        if change != "unchanged":
            security = replace(security, routing=replace(security.routing, run_id="second"))
        next_context = {**security.to_runtime_context(), "origin_message_id": "second-input"}
        if durable and change == "run_scope":
            with pytest.raises(RuntimeError, match="rebuild committed"):
                await graph.ainvoke({"messages": [HumanMessage(id="second-input", content="continue")]}, config,
                                   context=next_context, durability="sync")
            assert len(captured) == 1
            graph = await lead.make_lead_agent(model_name="fixture", tools=[], system_prompt="base",
                middlewares=[ImageAttachmentProjectionMiddleware()], frozen_contexts=contexts, attempt_middleware=PreparedFailure())
            graph.checkpointer = saver
            second = await graph.ainvoke(None, config, context=next_context, durability="sync")
        else:
            second = await graph.ainvoke({"messages": [HumanMessage(id="second-input", content="continue")]}, config,
                                        context=next_context, durability="sync")
        old_response = next(message for message in second["messages"] if message.id == response.id)
        if change == "unchanged":
            assert second["request_manifest"]["history_rebuild_reason"] is None
            assert old_response.additional_kwargs.get("focus_response_items")
            assert any(item.get("id") == "reason1" for item in captured[1]["input"])
        else:
            assert second["request_manifest"]["history_rebuild_reason"]
            assert "focus_response_items" not in old_response.additional_kwargs
            assert not any(item.get("id") == "reason1" for item in captured[1]["input"])
            assert any(message.id.startswith("execution-rebuild:") for message in second["messages"])
        assert (await graph.aget_state(old.config)).values == old_values
        assert old_values["messages"][-1].additional_kwargs.get("focus_response_items")
        synchronize_items(second["execution_items"], second["messages"])
        await client.aclose()
        model._client.close()
    asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop)


def test_explicit_rebuild_rejects_stale_source_hash():
    items = synchronize_items(None, [HumanMessage(id="task", content="task")])
    with pytest.raises(ValueError, match="源历史"):
        replace_execution_items(items, ExecutionHistoryRebuild(content_hash(None), items))
    assert replace_execution_items(items, ExecutionHistoryRebuild(content_hash(items), items)) == items


def test_retained_frozen_binding_without_new_authority_field_is_not_rewritten():
    context = FrozenContext("memory", "frozen version", ({"memory_id": "version-one"},))
    old = prepare_scoped_contexts([], (context,), "run")[0]
    old.additional_kwargs["focus_context"].pop("authority")
    before = old.model_dump()
    assert prepare_scoped_contexts([old], (context,), "run") == []
    assert old.model_dump() == before
    old.content = "changed version"
    with pytest.raises(ValueError, match="retained checkpoint"):
        prepare_scoped_contexts([old], (context,), "run")
