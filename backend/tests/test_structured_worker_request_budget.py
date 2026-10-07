"""本文件对外提供结构化 Worker 的实际请求预算回归。

输入为 78 来源合同、输出模式及离线 HTTP adapter；输出为实际请求与调用前估算一致的断言。
具体工作流为使用真实 Runnable 绑定及 SDK 序列化发送至 MockTransport，比较 schema/工具/消息与预算；没有外部模型调用。
示例：python -m pytest backend/tests/test_structured_worker_request_budget.py -q。
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_openai import ChatOpenAI

from backend.app.desktop.agent_loop import structured_worker
from backend.app.desktop.agent_loop.task_progress.candidate_contract import candidate_schema, validate_candidate
from backend.app.desktop.agent_loop.task_progress.candidate_interpretation import interpretation_payload
from backend.app.desktop.agent_loop.task_progress.runtime import _SYSTEM, TaskProgressRuntime
from focus.messages.request_budget import estimate_request_budget, estimate_responses_budget
from focus.models.provider_contract import ProviderContract
from focus.models.responses import FocusResponsesChatModel
from focus.models.deepseek import DeepSeekChatOpenAI
from test_task_progress_candidate_contract import covered_candidate, frozen_inputs


@pytest.mark.parametrize("protocol,provider", [("responses", "openai"), ("responses", "deepseek"),
                                              ("chat_completions", "openai"), ("chat_completions", "deepseek")])
@pytest.mark.parametrize("method", ["prompt_json", "json_schema", "json_mode", "function_calling"])
def test_estimate_matches_sdk_request_including_bound_output_contract(monkeypatch, protocol, provider, method):
    async def run():
        inputs = frozen_inputs()
        schema = candidate_schema(inputs)
        payload = interpretation_payload(inputs, {})
        candidate = covered_candidate(inputs).model_dump(mode="json")
        requests = []

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            content = json.dumps(candidate)
            if protocol == "responses":
                output = [{"type": "function_call", "id": "fc", "call_id": "call", "name": schema.__name__, "arguments": content}]
                if method != "function_calling":
                    output = [{"type": "message", "id": "m", "role": "assistant", "status": "completed",
                               "content": [{"type": "output_text", "text": content, "annotations": []}]}]
                result = {"object": "response", "id": "fixture", "created_at": 0, "model": "fixture", "status": "completed", "output": output}
            else:
                message = {"role": "assistant", "content": content}
                if method == "function_calling":
                    message = {"role": "assistant", "content": None, "tool_calls": [{"type": "function", "id": "call",
                        "function": {"name": schema.__name__, "arguments": content}}]}
                result = {"id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture",
                          "choices": [{"index": 0, "finish_reason": "stop", "message": message}]}
            return httpx.Response(200, json=result)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        options = {"model": "fixture", "api_key": "fixture", "base_url": "https://fixture.test/v1",
                   "max_tokens": 1000, "max_retries": 0, "http_async_client": client}
        chat_class = ChatOpenAI if provider == "openai" else DeepSeekChatOpenAI
        model = (FocusResponsesChatModel(**options, provider_contract=ProviderContract(provider=provider, protocol=protocol))
                 if protocol == "responses" else chat_class(**options))
        config = SimpleNamespace(name="fixture", model="fixture", curation_output_method=method,
                                 curation_max_output_tokens=1000, context_window=100000)
        app = SimpleNamespace(get_model=lambda name: config, resolve_default_model_name=lambda: "fixture")
        monkeypatch.setattr(structured_worker, "create_chat_model", lambda **kwargs: model)
        worker = structured_worker.StructuredWorkerModel(app)
        try:
            estimated = worker.estimate_input_tokens(schema, _SYSTEM, payload)
            assert not requests
            assert validate_candidate(inputs, await worker.invoke(schema, _SYSTEM, payload)) == covered_candidate(inputs)
            body = requests[0]
            if protocol == "responses":
                actual = estimate_responses_budget(body)
            else:
                actual = estimate_request_budget(body["messages"], format_spec={key: value for key, value in body.items() if key != "messages"})
            assert estimated == actual
            assert all(source.source_key in json.dumps(body) for source in inputs.task_delta.sources)
            if method == "json_schema":
                assert "json_schema" in str(body.get("text", body.get("response_format")))
            if method == "function_calling":
                assert body["tools"]
        finally:
            await client.aclose()
            model._client.close() if protocol == "responses" else model.client._client.close()
    asyncio.run(run())


@pytest.mark.parametrize("method", ["prompt_json", "json_schema", "json_mode", "function_calling"])
def test_full_request_window_rejects_before_reservation_or_invocation(monkeypatch, method):
    inputs = frozen_inputs()
    config = SimpleNamespace(name="fixture", model="fixture", curation_output_method=method,
                             curation_max_output_tokens=1000, context_window=10622)
    app = SimpleNamespace(get_model=lambda name: config, resolve_default_model_name=lambda: "fixture")
    model = FocusResponsesChatModel(model="fixture", api_key="fixture", max_tokens=1000,
                                   provider_contract=ProviderContract(provider="openai", protocol="responses"))
    monkeypatch.setattr(structured_worker, "create_chat_model", lambda **kwargs: model)
    worker = structured_worker.StructuredWorkerModel(app)
    schema = candidate_schema(inputs)
    payload = interpretation_payload(inputs, {})
    config.context_window = worker.estimate_input_tokens(schema, _SYSTEM, payload) + 999
    runtime = TaskProgressRuntime(None, app, model_factory=lambda name: worker)

    async def forbidden(*args):
        pytest.fail("完整请求超窗时不得预留或调用模型")

    monkeypatch.setattr(runtime, "_reserve", forbidden)
    monkeypatch.setattr(worker, "invoke", forbidden)
    try:
        with pytest.raises(ValueError, match="context_budget"):
            asyncio.run(runtime._interpret(inputs, 1, {}, None, payload))
    finally:
        model._client.close()
