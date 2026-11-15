"""真实 Portfolio Patrol 的搜索、抓取、失败恢复与预算边界离线验收。

保留生产模型适配器、结构化认知和内置工具执行，仅替换模型及 DeepSeek HTTP 传输。
"""

import asyncio
import json

import httpx
import pytest
from focus.config import app_config as config_module
from focus.models.factory import create_chat_model
from focus.tools.builtins import web_tools
from pydantic import ValidationError

from backend.app.desktop.patrol.agent_loop.rounds import round_orchestration as module
from backend.app.desktop.patrol.agent_loop.rounds.round_orchestration import (
    PatrolCognitiveStep,
    StructuredPatrolDecisionModel,
)
from backend.tests.config_helpers import app_config_for
from backend.tests.test_patrol_failure_diagnostics import _observation


@pytest.mark.parametrize("method", ["prompt_json", "json_schema"])
@pytest.mark.parametrize("search_status", [200, 401])
def test_portfolio_patrol_search_fetch_then_validated_decision(monkeypatch, method, search_status):
    async def run():
        requests, web_requests = [], []
        url = "https://docs.python.org/3/library/asyncio.html"
        config = app_config_for("patrol-web", None)
        config.models[0].protocol = "responses"
        config.models[0].curation_output_method = method
        monkeypatch.setattr(config_module, "_app_config", config)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-web-key")
        outputs = [
            {"web_tools": [{"name": "web_search", "arguments": {"query": "Python asyncio"}}]},
            {"web_tools": [{"name": "web_fetch", "arguments": {"url": url}}]},
            {"decision": {"rationale": "使用联网参考继续工作", "evidence": [{"url": url}],
                "mission_references": [{"role": "outcome", "reference_id": "outcome"}],
                "actions": [{"action": "continue_context", "context_id": "context",
                    "context_revision_id": "revision", "message": "依据参考继续检查"}]}},
        ]

        def model_response(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if len(requests) > 1:
                evidence = next(item["content"] for item in reversed(payload["input"])
                    if item.get("role") == "user" and "<selected_context_evidence>" in str(item.get("content")))
                if len(requests) == 2:
                    assert (url if search_status == 200 else "HTTP 401") in str(evidence)
                else:
                    assert "Actual fetched documentation" in str(evidence)
            return httpx.Response(200, json={"id": f"response-{len(requests)}", "object": "response",
                "created_at": 0, "status": "completed", "model": "patrol-web",
                "output": [{"id": "message", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": json.dumps(outputs[len(requests) - 1]), "annotations": []}]}],
                "usage": {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}})

        def web_response(request):
            web_requests.append(request)
            assert request.headers["Authorization"] == "Bearer fixture-web-key"
            if request.url.path.endswith("search"):
                assert json.loads(request.content) == {"query": "Python asyncio", "count": 5, "need_content": False}
                return httpx.Response(search_status, json={"results": [{"title": "asyncio", "url": url, "summary": "Async I/O"}]})
            assert json.loads(request.content) == {"url": url}
            return httpx.Response(200, json={"content": "# Actual fetched documentation"})

        real_client = httpx.AsyncClient
        client = real_client(transport=httpx.MockTransport(model_response))
        monkeypatch.setattr(module, "create_chat_model", lambda **kwargs: create_chat_model(**kwargs, http_async_client=client))
        monkeypatch.setattr(web_tools, "AsyncClient", lambda **kwargs: real_client(**kwargs,
            transport=httpx.MockTransport(web_response), trust_env=False))
        observation = _observation()
        original = observation.model_dump_json()
        patrol = StructuredPatrolDecisionModel(config)
        try:
            intent = await patrol(observation)
            assert intent.actions[0].action == "continue_context"
            assert (intent.loop_id, intent.round_id, intent.grant_id) == ("loop", "round", "grant")
            assert intent.evidence == ({"url": url}, {"role": "outcome", "reference_id": "outcome", "kind": "mission_reference"})
            assert len(requests) == patrol.call_count == patrol.usage.model_calls == 3
            assert (patrol.usage.input_tokens, patrol.usage.output_tokens) == (120, 30)
            assert len(patrol.attempt_metadata) == 3 and len(web_requests) == 2
            assert observation.model_dump_json() == original
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    {"web_tools": [{"name": "bash", "arguments": {"command": "pwd"}}]},
    {"web_tools": [{"name": "web_fetch", "arguments": {}}]},
    {"web_tools": [{"name": "web_search", "arguments": {"query": "x"}}] * 2},
    {"web_tools": [{"name": "web_search", "arguments": {"query": "x"}}], "reads": [{"context_id": "c", "revision_id": "r"}]},
])
def test_patrol_web_requests_reject_unknown_tools_missing_arguments_and_mixed_steps(payload):
    with pytest.raises(ValidationError):
        PatrolCognitiveStep.model_validate(payload)


def test_patrol_does_not_fetch_without_budget_for_a_followup_decision(monkeypatch):
    async def unexpected(request):
        raise AssertionError("不得执行联网请求")

    monkeypatch.setattr(module, "read_web_evidence", unexpected)
    model = StructuredPatrolDecisionModel(object())
    model.call_count = model._remaining_calls = 1
    step = PatrolCognitiveStep.model_validate({"web_tools": [{"name": "web_fetch", "arguments": {"url": "https://example.com"}}]})
    with pytest.raises(RuntimeError, match="budget 已耗尽"):
        asyncio.run(model._cognitive_evidence(_observation(), step))
