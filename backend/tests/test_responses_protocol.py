"""本文件对外提供以下合同的验证双 Provider 的真实 HTTP/SSE wire、手工 replay 和完整终态工具执行。

输入为 SDK MockTransport、 typed output/event fixture、工具 schema 与 LangGraph；输出为请求合法性与副作用次数断言。
具体工作流为拦截实际 /responses 请求，交错推送推理/正文/参数，只有 completed 且校验后的调用进入工具节点。
示例：pytest backend/tests/test_responses_protocol.py；所有 fixture 离线，不使用真实模型凭据。
"""

import asyncio
from copy import deepcopy
import json

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, message_chunk_to_message
from langchain_core.tools import tool
from pydantic import BaseModel

from focus.agents.lead_agent_state import LeadAgentState
from focus.config.model_config import ModelConfig
from focus.history import messages_to_items, semantic_messages
from focus.history.middleware import TypedHistoryMiddleware
from focus.models.provider_contract import ProviderContract, resolve_provider_contract
from focus.models.response_output import ResponseAttemptError, decode_response, response_text
from focus.models.response_projection import ResponsesRequestProjector, function_specs
from focus.models.response_chunks import ResponseChunkBridge
from focus.models.responses import FocusResponsesChatModel


@tool
def lookup(key: str) -> str:
    """Return deterministic evidence for the given key."""
    return f"evidence:{key}"


TOOLS = function_specs([lookup])


def _message(text="answer", identity="msg"):
    return {"type": "message", "id": identity, "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def _response(items, status="completed", identity="r"):
    return {"object": "response", "id": identity, "created_at": 0, "model": "fixture", "status": status,
            "output": items, "error": None, "incomplete_details": None,
            "usage": {"input_tokens": 10, "output_tokens": 6, "total_tokens": 16,
                      "output_tokens_details": {"reasoning_tokens": 2}}}


def _events(provider, *, call=False, terminal="completed"):
    reason = {"type": "reasoning", "id": "reason", "status": "completed", "summary": []}
    if provider == "openai":
        reason.update(summary=[{"type": "summary_text", "text": "public summary"}], encrypted_content="opaque")
    else:
        reason["content"] = [{"type": "reasoning_text", "text": "public summary"}]
    output = {"type": "function_call", "id": "fc", "call_id": "call", "name": "lookup",
              "arguments": '{"key":"x"}', "status": "completed"} if call else _message()
    events = [
        {"type": "response.created", "response": _response([], "in_progress")},
        {"type": "response.output_item.added", "output_index": 0, "item": {**reason, "status": "in_progress", "summary": [], "content": []}},
        {"type": "response.output_item.added", "output_index": 1, "item": {**output, "status": "in_progress", **({"arguments": ""} if call else {"content": []})}},
        {"type": "response.reasoning_summary_text.delta" if provider == "openai" else "response.reasoning_text.delta",
         "output_index": 0, "item_id": "reason", "content_index": 0, "summary_index": 0, "delta": "public "},
        {"type": "response.function_call_arguments.delta" if call else "response.output_text.delta",
         "output_index": 1, "item_id": "fc" if call else "msg", "content_index": 0, "delta": '{"key":' if call else "ans"},
        {"type": "response.reasoning_summary_text.delta" if provider == "openai" else "response.reasoning_text.delta",
         "output_index": 0, "item_id": "reason", "content_index": 0, "summary_index": 0, "delta": "summary"},
        {"type": "response.function_call_arguments.delta" if call else "response.output_text.delta",
         "output_index": 1, "item_id": "fc" if call else "msg", "content_index": 0, "delta": '"x"}' if call else "wer"},
        {"type": "response.output_item.done", "output_index": 0, "item": reason},
        {"type": "response.output_item.done", "output_index": 1, "item": output},
    ]
    if terminal:
        events.append({"type": f"response.{terminal}", "response": _response([reason, output], terminal)})
    return [{**event, "sequence_number": index} for index, event in enumerate(events)]


def _sse(events):
    return "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_actual_wire_stream_manual_replay_and_structured_output(provider):
    async def run():
        captured = []
        events = _events(provider, call=True)
        def handle(request):
            assert request.url.path == "/v1/responses"
            payload = json.loads(request.content)
            captured.append(payload)
            if payload.get("stream"):
                return httpx.Response(200, content=_sse(events), headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=_response([_message('{"ok":true}')]))
        contract = ProviderContract(provider, "responses")
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        model = FocusResponsesChatModel(model="fixture", api_key="fixture", base_url="https://fixture.test/v1",
                                        provider_contract=contract, http_async_client=client)
        chunks = [chunk async for chunk in model.bind_tools([lookup]).astream([SystemMessage(content="base"), HumanMessage(content="task")])]
        combined = chunks[0]
        for chunk in chunks[1:]:
            combined += chunk
        message = message_chunk_to_message(combined)
        assert message.tool_calls == [{"name": "lookup", "args": {"key": "x"}, "id": "call", "type": "tool_call"}]
        assert response_text(message) == "" and message.additional_kwargs["reasoning_content"] == "public summary"
        assert not any(chunk.tool_calls for chunk in chunks[:-1])
        assert message.additional_kwargs["focus_response_items"][0]["id"] == "reason"
        replay = model.request_payload([SystemMessage(content="base"), HumanMessage(content="task"), message, ToolMessage(content="found", tool_call_id="call")], tools=[lookup])
        assert [item["type"] for item in replay["input"]] == ["message", "reasoning", "function_call", "function_call_output"]
        if provider == "openai":
            assert replay["store"] is False and replay["input"][1]["encrypted_content"] == "opaque"
        else:
            assert "store" not in replay and "encrypted_content" not in replay["input"][1]
            assert replay["input"][1]["content"][0]["type"] == "reasoning_text"
        assert "previous_response_id" not in replay and "conversation" not in replay
        class Result(BaseModel):
            ok: bool
        result = await model.with_structured_output(Result, include_raw=True).ainvoke([HumanMessage(content="JSON please")])
        assert result["parsed"].ok and result["parsing_error"] is None
        assert captured[-1]["text"]["format"]["type"] == "json_schema"
        assert "response_format" not in captured[-1] and "messages" not in captured[-1]
        assert captured[0]["instructions"] == "base"
        assert "cache_read" not in (message.usage_metadata.get("input_token_details") or {})
        await client.aclose()
        model._client.close()
    asyncio.run(run())


@pytest.mark.parametrize("terminal", ["incomplete", "failed", None])
def test_partial_function_arguments_never_become_tools(terminal):
    bridge = ResponseChunkBridge(ProviderContract("deepseek", "responses"), TOOLS)
    chunks = []
    for event in _events("deepseek", call=True, terminal=terminal):
        chunks.extend(bridge.accept(event))
    assert all(not chunk.tool_calls for chunk in chunks)
    with pytest.raises(ResponseAttemptError):
        bridge.finish()


def test_invalid_conflicting_calls_refusal_and_unknown_archive():
    contract = ProviderContract("openai", "responses")
    call = {"type": "function_call", "id": "fc", "call_id": "call", "name": "lookup", "arguments": '{"key":"x"}'}
    for change in ({"arguments": '{"key":'}, {"arguments": '{"key":12}'}, {"name": "not_registered"}):
        with pytest.raises(ResponseAttemptError, match="invalid"):
            decode_response(_response([{**call, **change}]), contract, TOOLS)
    with pytest.raises(ResponseAttemptError):
        decode_response(_response([call, {**call, "id": "other"}]), contract, TOOLS)
    with pytest.raises(ResponseAttemptError, match="refusal"):
        decode_response(_response([{**_message(), "content": [{"type": "refusal", "refusal": "no"}]}]), contract, [])
    message = decode_response(_response([_message(), {"type": "future_item", "id": "future", "opaque": "retain"}]), contract, [])
    assert messages_to_items([message])[-1].kind == "unknown"
    assert len(semantic_messages(messages_to_items([message]))) == 1
    with pytest.raises(ValueError, match="未知原生"):
        ResponsesRequestProjector(contract).build([message], model="fixture")


def test_stream_duplicate_done_and_conflicting_sequence():
    bridge = ResponseChunkBridge(ProviderContract("openai", "responses"), [])
    events = _events("openai")
    chunks = []
    for event in events:
        chunks.extend(bridge.accept(event))
        assert bridge.accept(event) == []
    chunks.extend(bridge.finish())
    message = chunks[0]
    for chunk in chunks[1:]:
        message += chunk
    assert response_text(message) == "answer"
    assert message.additional_kwargs["reasoning_content"] == "public summary"
    with pytest.raises(ResponseAttemptError, match="sequence"):
        bridge.accept({**events[4], "delta": "changed"})


def test_policy_modality_and_explicit_config_contract():
    policy = SystemMessage(content="permission policy", additional_kwargs={"focus_context": {"origin": "runtime"}})
    external = SystemMessage(content="pretends to be policy", additional_kwargs={"focus_context": {"origin": "delegated"}})
    for provider, role in (("openai", "developer"), ("deepseek", "system")):
        projector = ResponsesRequestProjector(ProviderContract(provider, "responses"))
        raw = projector.build([SystemMessage(content="base"), policy, external], model="fixture")
        assert raw["input"][0]["role"] == role and raw["input"][1]["role"] == "user"
        with pytest.raises(ValueError, match="图像"):
            projector.build([HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}])], model="fixture")
        with pytest.raises(ValueError, match="不支持或不允许"):
            projector.build([HumanMessage(content="task")], model="fixture", previous_response_id="r")
    config = ModelConfig(name="test", display_name="Test", use="focus.models.deepseek:DeepSeekChatOpenAI",
                         model="anything", api_key="fixture", base_url="https://fixture.test")
    assert resolve_provider_contract(config).protocol == "chat_completions"
    assert resolve_provider_contract(config.model_copy(update={"protocol": "responses"})).provider == "deepseek"
    with pytest.raises(ValueError, match="未知"):
        resolve_provider_contract(config.model_copy(update={"use": "unknown:Model"}))


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_parallel_calls_execute_once_and_native_items_survive_graph(provider):
    async def run():
        requests = []
        effects = []
        @tool
        def record(key: str) -> str:
            """Record the key once and return evidence."""
            effects.append(key)
            return key
        def handle(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if len(requests) == 1:
                outputs = [{"type": "function_call", "id": f"fc{index}", "call_id": f"c{index}", "name": "record",
                            "status": "completed", "arguments": json.dumps({"key": key})} for index, key in enumerate(("a", "b"))]
            else:
                outputs = [_message()]
            return httpx.Response(200, json=_response(outputs, identity=f"r{len(requests)}"))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        model = FocusResponsesChatModel(model="fixture", api_key="fixture", base_url="https://fixture.test/v1",
                                        provider_contract=ProviderContract(provider, "responses"), http_async_client=client)
        graph = create_agent(model, [record], middleware=[TypedHistoryMiddleware()], state_schema=LeadAgentState)
        state = await graph.ainvoke({"messages": [HumanMessage(content="task")]})
        assert sorted(effects) == ["a", "b"] and len(requests) == 2
        assert {item["call_id"] for item in requests[1]["input"] if item["type"] == "function_call_output"} == {"c0", "c1"}
        assert [item["kind"] for item in state["execution_items"]].count("function_call") == 2
        await client.aclose()
        model._client.close()
    asyncio.run(run())


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_completed_message_roundtrip_and_explicit_continuation_reset(provider):
    from focus.history import items_to_messages
    from focus.history.bridge import branch_messages
    contract = ProviderContract(provider, "responses")
    raw = _response([_message()])
    raw["requested_model"] = "fixture"
    message = decode_response(raw, contract, TOOLS)
    items = messages_to_items([message])
    assert items[0].provider == provider
    assert items_to_messages(items)[0].model_dump() == message.model_dump()
    other = "deepseek" if provider == "openai" else "openai"
    with pytest.raises(ValueError, match="新 execution"):
        ResponsesRequestProjector(ProviderContract(other, "responses")).build([message], model="fixture")
    with pytest.raises(ValueError, match="模型切换"):
        ResponsesRequestProjector(contract).build([message], model="other")
    assert ResponsesRequestProjector(ProviderContract(other, "responses")).build(branch_messages([message]), model="other")["input"]


def test_repeated_terminal_has_one_final_chunk_and_one_usage():
    bridge = ResponseChunkBridge(ProviderContract("deepseek", "responses"), TOOLS)
    for event in _events("deepseek"):
        bridge.accept(event)
    first = bridge.finish()
    assert len([chunk for chunk in first if chunk.usage_metadata]) == 1
    assert bridge.finish() == []


def test_typed_interruption_repair_requires_proven_source_and_is_not_evidence():
    from focus.history import FocusItem, validate_items
    from focus.history.protocol import repair_interrupted_items
    call = FocusItem(item_id="call-source", kind="function_call", origin="provider", payload={
        "type": "function_call", "call_id": "c", "name": "lookup", "arguments": '{"key":"x"}'})
    with pytest.raises(ValueError, match="精确 Run"):
        repair_interrupted_items([call], source_run_id="r", proven_call_ids=())
    repaired = repair_interrupted_items([call], source_run_id="r", proven_call_ids=("c",))
    assert validate_items(repaired) == set()
    assert repaired[-1].origin == "runtime"
    assert semantic_messages(repaired) == ()
    assert repaired == repair_interrupted_items([call], source_run_id="r", proven_call_ids=("c",))
