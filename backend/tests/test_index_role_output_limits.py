"""角色输出上限实际进入 provider 请求、准入与缓存 identity，默认合同兼容。"""

import asyncio
import hashlib
import json

import httpx
import pytest
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from backend.app.desktop.patrol.agent_loop.workers import structured_worker
from backend.app.desktop.patrol.agent_loop.workers.derivation_worker import RoleBoundStructuredModel
from backend.tests.config_helpers import app_config_for


def test_role_limit_changes_only_its_effective_cache_contract():
    config = app_config_for("fixture", None)
    projector = RoleBoundStructuredModel(config, "semantic_index_projector")
    interpreter = RoleBoundStructuredModel(config, "semantic_index_interpreter")
    verifier = RoleBoundStructuredModel(config, "claim_verifier")
    model = config.models[0]
    old_payload = {k: v for k, v in model.model_dump(mode="json").items()
                   if k not in {"api_key", "display_name", "default", "curation_default", "derivation_max_output_tokens"}}
    original = hashlib.sha256(json.dumps(old_payload, sort_keys=True).encode()).hexdigest()
    assert projector.cache_identity == interpreter.cache_identity == verifier.cache_identity == original
    model.derivation_max_output_tokens = {"semantic_index_interpreter": 2048}
    assert projector.cache_identity == verifier.cache_identity == original
    assert interpreter.cache_identity != original and interpreter.max_output_tokens == 2048


@pytest.mark.parametrize("role,limit", [("semantic_index_projector", 4096),
                                       ("semantic_index_interpreter", 2048), ("claim_verifier", 1024)])
def test_effective_role_limit_is_used_by_guard_request_and_receipt(monkeypatch, role, limit):
    class Result(BaseModel):
        value: int

    async def run():
        config = app_config_for("fixture", None)
        config.models[0].curation_output_method = "prompt_json"
        config.models[0].derivation_max_output_tokens = {role: limit}
        sent, admitted, reserved = [], [], []

        def handle(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0,
                "model": "fixture", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"value":1}'}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        chats = []

        def create(**kwargs):
            chat = ChatOpenAI(model="fixture", api_key="test", base_url="https://fixture.test/v1",
                              max_tokens=kwargs["max_tokens"], max_retries=0, http_async_client=client)
            chats.append(chat)
            return chat

        class Receipts:
            async def reserve(self, input_tokens, output_tokens, **kwargs):
                reserved.append(output_tokens)
                return "receipt"

            async def settle(self, receipt, measured, reported):
                assert reported and measured.output_tokens == 5

        async def guard(worker, *args):
            admitted.append(worker.max_output_tokens)

        monkeypatch.setattr(structured_worker, "create_chat_model", create)
        model = RoleBoundStructuredModel(config, role)
        model.bind_request_guard(guard)
        model.bind_usage_receipts(Receipts())
        try:
            assert (await model.invoke(Result, "test", {})).value == 1
            assert admitted == reserved == [limit]
            assert sent[0].get("max_completion_tokens", sent[0].get("max_tokens")) == limit
            assert model.last_attempt_records[0]["output_tokens"] == 5
        finally:
            await client.aclose()
            for chat in chats:
                chat.client._client.close()

    asyncio.run(run())


@pytest.mark.parametrize("value", [0, 511, 65537])
def test_invalid_role_limits_are_rejected(value):
    from focus.config.model_config import ModelConfig

    payload = app_config_for("fixture", None).models[0].model_dump()
    payload["derivation_max_output_tokens"] = {"claim_verifier": value}
    with pytest.raises(ValidationError):
        ModelConfig.model_validate(payload)
