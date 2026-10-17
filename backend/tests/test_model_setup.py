"""首次配置与修复验收：真实装配、HTTP 序列化、策展解析及设置 API，不调用真实账号。"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from focus.config import app_config as config_module
from focus.config.app_config import get_app_config
from backend.app.desktop.model_settings import (
    ModelSettingsError, save_model_settings, settings_snapshot,
)
from backend.app.desktop.model_settings_routes import model_settings_router
from backend.app.desktop.model_verification import verify_model_setup


@pytest.fixture
def setup_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / ".focus"))
    monkeypatch.setenv("SETUP_TEST_KEY", "old-synthetic-key")
    monkeypatch.setattr(config_module, "_app_config", None)
    entry = {
        "name": "test", "display_name": "Test", "use": "langchain_openai:ChatOpenAI",
        "model": "test", "api_key": "$SETUP_TEST_KEY", "base_url": "https://example.test/v1",
        "default": True, "curation_default": True, "curation_output_method": "prompt_json",
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"models": [entry]}), encoding="utf-8")
    return tmp_path, entry


@pytest.fixture
def provider(monkeypatch):
    """只替换 HTTP 传输，保留真实客户端、协议映射及策展引擎。"""
    from focus.models import create_chat_model

    state = {"calls": [], "status": 200, "curation": None, "semantic_index": None, "network_error": None}
    def respond(request):
        payload = json.loads(request.content)
        state["calls"].append((request.headers.get("authorization"), payload))
        if state["network_error"]:
            raise state["network_error"]("synthetic network failure", request=request)
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"error": {"message": "rejected synthetic-new-key"}})
        is_curation = "Focus Context Curator" in json.dumps(payload)
        content = state["curation"] if is_curation else "OK"
        if content is None:
            content = json.dumps({"outcome": "replace", "items": [{"type": "copy_message", "source_message_id": "probe"}]})
        if "你是无权 semantic_index_projector" in json.dumps(payload, ensure_ascii=False):
            content = state["semantic_index"]
            if content is None:
                content = json.dumps({"units": [{"kind": "claim", "authority": "hypothesis",
                    "statement": "请保留这条测试消息。",
                    "supports": [{"message_id": "probe", "quote": "请保留这条测试消息。"}]}]})
        if "input" in payload:
            return httpx.Response(200, json={
                "id": "probe", "object": "response", "created_at": 0, "model": "test", "status": "completed",
                "output": [{"type": "message", "role": "assistant", "id": "msg-probe", "status": "completed",
                            "content": [{"type": "output_text", "text": content, "annotations": []}]}],
            })
        return httpx.Response(200, json={
            "id": "probe", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        })
    transport = httpx.MockTransport(respond)
    def factory(*args, **kwargs):
        return create_chat_model(*args, **kwargs, http_client=httpx.Client(transport=transport),
                                 http_async_client=httpx.AsyncClient(transport=transport))
    monkeypatch.setattr("focus.models.create_chat_model", factory)
    monkeypatch.setattr("backend.app.desktop.context_curator.engine.create_chat_model", factory)
    monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", factory)
    return state


@pytest.mark.parametrize("key", [None, "", "sk-your-api-key-here", "your_api_key"])
def test_first_launch_requests_setup_for_missing_or_placeholder_key(setup_workspace, monkeypatch, key):
    if key is None:
        monkeypatch.delenv("SETUP_TEST_KEY")
    else:
        monkeypatch.setenv("SETUP_TEST_KEY", key)
    snapshot = settings_snapshot(get_app_config("config.yaml"))
    assert snapshot["setup_required"] is True
    assert snapshot["models"][0]["api_key_set"] is False
    assert "models[0].api_key" in snapshot["setup_issues"]


def test_invalid_model_directory_can_be_read_repaired_and_applied(setup_workspace, provider):
    path, entry = setup_workspace
    invalid = {**entry, "protocol": "broken", "curation_output_method": "yaml"}
    (path / "config.yaml").write_text(yaml.safe_dump({"models": [invalid]}), encoding="utf-8")
    with pytest.raises(ValidationError):
        get_app_config("config.yaml")
    config = get_app_config("config.yaml", recover_models=True)
    snapshot = settings_snapshot(config)
    assert snapshot["models"][0]["protocol"] == "broken"
    assert snapshot["setup_required"]
    assert "old-synthetic-key" not in json.dumps(snapshot)
    client = _client(config)
    response = client.put("/desktop/api/settings/models", json={
        "models": [entry], "credentials": {"SETUP_TEST_KEY": "synthetic-new-key"},
    })
    assert response.status_code == 200, response.text
    assert response.json()["verification"]["ok"]
    assert not response.json()["setup_required"]
    assert config.models[0].api_key == "synthetic-new-key"
    assert not config._model_recovery
    assert get_app_config("config.yaml") is config
    assert all(auth == "Bearer synthetic-new-key" for auth, _ in provider["calls"])
    # 模拟重启重新加载两层配置，修复结果不能只停留在内存。
    from focus.config.app_config import _load_layered_config
    reloaded = _load_layered_config("config.yaml")
    assert reloaded.models[0].protocol == entry.get("protocol")
    assert reloaded.models[0].api_key == "synthetic-new-key"


def _client(config):
    app = FastAPI()
    app.include_router(model_settings_router)
    app.state.desktop_service = SimpleNamespace(app_config=config, session_factory=None)
    return TestClient(app)


@pytest.mark.parametrize("method", ["prompt_json", "json_mode", "json_schema"])
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_saved_key_reaches_chat_and_real_curation_output_path(setup_workspace, provider, method, protocol):
    _, entry = setup_workspace
    entry["curation_output_method"] = method
    if protocol == "responses":
        entry.update(use="focus.models.responses:FocusResponsesChatModel", protocol=protocol, provider="deepseek")
    config = get_app_config("config.yaml")
    client = _client(config)
    response = client.put("/desktop/api/settings/models", json={
        "models": [entry], "credentials": {"SETUP_TEST_KEY": "synthetic-new-key"},
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["verification"]["ok"], result
    assert [check["capability"] for check in result["verification"]["checks"]] == ["chat", "curation", "semantic_index"]
    assert len(provider["calls"]) == 3
    assert ("input" in provider["calls"][1][1]) == (protocol == "responses")
    assert all(auth == "Bearer synthetic-new-key" for auth, _ in provider["calls"])
    assert "synthetic-new-key" not in response.text
    assert client.get("/desktop/api/settings/models").json()["verification"]["ok"]


@pytest.mark.parametrize("status,diagnostic", [(401, "密钥"), (403, "权限"), (404, "模型"), (400, "协议"), (429, "重试")])
def test_failed_verification_keeps_settings_editable_with_redacted_diagnosis(setup_workspace, provider, status, diagnostic):
    _, entry = setup_workspace
    provider["status"] = status
    client = _client(get_app_config("config.yaml"))
    response = client.put("/desktop/api/settings/models", json={
        "models": [entry], "credentials": {"SETUP_TEST_KEY": "synthetic-new-key"},
    })
    result = response.json()
    assert response.status_code == 200
    assert not result["verification"]["ok"]
    assert diagnostic in result["verification"]["reason"]
    assert "synthetic-new-key" not in response.text
    assert client.get("/desktop/api/settings/models").status_code == 200


def test_chat_success_does_not_hide_invalid_curation_output(setup_workspace, provider):
    provider["curation"] = "not json"
    result = asyncio.run(verify_model_setup(get_app_config("config.yaml")))
    assert result["checks"][0]["ok"]
    assert not result["checks"][1]["ok"]
    assert "策展输出" in result["reason"]


def test_different_chat_and_curation_defaults_use_their_own_credentials(setup_workspace, provider, monkeypatch):
    _, entry = setup_workspace
    monkeypatch.delenv("SETUP_CURATION_KEY", raising=False)
    curation = {**entry, "name": "curator", "model": "curation-model", "default": False,
                "api_key": "$SETUP_CURATION_KEY"}
    config = get_app_config("config.yaml")
    saved = _client(config).put("/desktop/api/settings/models", json={
        "models": [{**entry, "curation_default": False}, curation],
        "credentials": {"SETUP_CURATION_KEY": "synthetic-curation-key"},
    })
    assert saved.json()["verification"]["ok"]
    assert provider["calls"][0][0] == "Bearer old-synthetic-key"
    assert provider["calls"][1][0] == "Bearer synthetic-curation-key"
    assert provider["calls"][1][1]["model"] == "curation-model"
    assert provider["calls"][2][0] == "Bearer synthetic-curation-key"
    assert provider["calls"][2][1]["model"] == "curation-model"


@pytest.mark.parametrize("role", ["semantic_index_projector", "claim_verifier"])
def test_automatic_index_roles_use_curation_default(setup_workspace, provider, role):
    from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
    from backend.app.desktop.agent_loop.patrol_runtime import CuratorCoordinationStage
    from backend.app.desktop.context_curator.contract import CuratedContextPlan
    from backend.app.desktop.context_curator.engine import CURATOR_SYSTEM_CONTRACT

    _, entry = setup_workspace
    config = get_app_config("config.yaml")
    saved = _client(config).put("/desktop/api/settings/models", json={
        "models": [
            {**entry, "curation_default": False, "curation_output_method": None},
            {**entry, "name": "curator", "model": "curation-model", "default": False,
             "context_window": 16384, "curation_max_output_tokens": 2048},
        ],
    })
    assert saved.json()["verification"]["ok"]
    service = CuratorCoordinationStage(None, object(), app_config=config)._index_service
    factory = (service._semantic_projector_factory if role == "semantic_index_projector"
               else service._semantic_claim_verifier_factory)
    worker = factory()
    assert worker.context_window_tokens == 16384
    assert worker.max_output_tokens == 2048
    assert worker.cache_identity == RoleBoundStructuredModel(config, role, "curator").cache_identity
    result = asyncio.run(worker.invoke(CuratedContextPlan, CURATOR_SYSTEM_CONTRACT, {}))
    assert result.outcome == "replace"
    assert provider["calls"][-1][1]["model"] == "curation-model"


@pytest.mark.parametrize("output", ["not json", '{"units": []}'])
def test_verification_rejects_invalid_automatic_index_output(setup_workspace, provider, output):
    provider["semantic_index"] = output
    result = asyncio.run(verify_model_setup(get_app_config("config.yaml")))
    assert not result["ok"]
    assert all(check["ok"] for check in result["checks"][:2])
    assert result["checks"][2]["capability"] == "semantic_index"
    assert not result["checks"][2]["ok"]
    assert "自动索引" in result["reason"]


def test_internal_model_selection_honors_explicit_overrides_and_rejects_missing_default(setup_workspace, monkeypatch):
    from backend.app.desktop.agent_loop.derivation_worker import RoleBoundStructuredModel
    from backend.app.desktop.agent_loop.structured_worker import StructuredWorkerModel
    from backend.app.desktop.context_curator.engine import CurationEngine

    config = get_app_config("config.yaml")
    curator = config.models[0].model_copy(update={"name": "curator", "default": False})
    config.models[0].curation_default = False
    config.models.append(curator)
    monkeypatch.setenv("FOCUS_MODEL", "test")
    assert config.resolve_default_model_name() == "test"
    assert config.resolve_curation_model_name() == "curator"
    assert StructuredWorkerModel(config).request_model_config.name == "curator"
    assert CurationEngine(config)._resolve_config(None).name == "curator"
    assert StructuredWorkerModel(config, "test").request_model_config.name == "test"
    default = RoleBoundStructuredModel(config, "claim_verifier")
    assert default.cache_identity == RoleBoundStructuredModel(config, "claim_verifier", "curator").cache_identity
    original_identity = default.cache_identity
    config.models[0].curation_default = True
    curator.curation_default = False
    assert default.cache_identity != original_identity
    assert default.cache_identity == RoleBoundStructuredModel(config, "claim_verifier", "test").cache_identity
    config.models[0].curation_default = False
    with pytest.raises(ValueError, match="策展默认模型"):
        _ = StructuredWorkerModel(config).request_model_config
    with pytest.raises(ValueError, match="策展默认模型"):
        _ = default.cache_identity
    assert StructuredWorkerModel(config, "test").request_model_config.name == "test"


def test_responses_protocol_mismatch_has_an_actionable_diagnosis(setup_workspace, monkeypatch):
    from focus.models.response_output import ResponseAttemptError
    def fail(**kwargs):
        raise ResponseAttemptError("invalid", {}, "synthetic protocol mismatch")
    monkeypatch.setattr("focus.models.create_chat_model", fail)
    monkeypatch.setattr("backend.app.desktop.context_curator.engine.create_chat_model", fail)
    monkeypatch.setattr("backend.app.desktop.agent_loop.structured_worker.create_chat_model", fail)
    result = asyncio.run(verify_model_setup(get_app_config("config.yaml")))
    assert not result["checks"][0]["ok"]
    assert "协议" in result["checks"][0]["reason"]
    assert "端点地址" in result["checks"][0]["reason"]


@pytest.mark.parametrize("error,diagnostic", [(httpx.ConnectError, "网络"), (httpx.ReadTimeout, "超时")])
def test_network_failures_offer_recovery(setup_workspace, provider, error, diagnostic):
    provider["network_error"] = error
    result = asyncio.run(verify_model_setup(get_app_config("config.yaml")))
    assert not result["ok"]
    assert diagnostic in result["reason"]


def test_placeholder_cannot_be_saved_as_ready(setup_workspace):
    _, entry = setup_workspace
    with pytest.raises(ModelSettingsError):
        save_model_settings(get_app_config("config.yaml"), {
            "models": [entry], "credentials": {"SETUP_TEST_KEY": "sk-your-api-key-here"},
        })


def test_literal_credentials_are_ready_and_never_appear_in_any_settings_section(setup_workspace):
    path, entry = setup_workspace
    entry["api_key"] = "synthetic-inline-secret"
    (path / "config.yaml").write_text(yaml.safe_dump({"models": [entry]}), encoding="utf-8")
    snapshot = settings_snapshot(get_app_config("config.yaml"))
    assert not snapshot["setup_required"]
    assert "synthetic-inline-secret" not in json.dumps(snapshot)


def test_saving_models_preserves_effective_non_model_runtime_settings(setup_workspace, provider):
    _, entry = setup_workspace
    from focus.config.database_config import DatabaseConfig
    config = get_app_config("config.yaml")
    config.database = DatabaseConfig(backend="postgres", url="postgresql+asyncpg://localhost/effective")
    config.context_run_admission = False
    save_model_settings(config, {"models": [entry], "credentials": {"SETUP_TEST_KEY": "synthetic-new-key"}})
    assert config.database.url == "postgresql+asyncpg://localhost/effective"
    assert not config.context_run_admission


def test_empty_runtime_catalog_does_not_resurrect_disk_entries(setup_workspace):
    config = get_app_config("config.yaml")
    config.models = []
    assert settings_snapshot(config)["models"] == []


def test_write_failure_restores_credentials_config_and_runtime(setup_workspace, monkeypatch):
    from focus.config import preference_store
    path, entry = setup_workspace
    home = path / ".focus"
    home.mkdir()
    (home / ".env").write_text("# keep\nSETUP_TEST_KEY=old-synthetic-key\n", encoding="utf-8")
    before = (home / ".env").read_bytes()
    config = get_app_config("config.yaml")
    def fail(*args):
        raise preference_store.PreferenceWriteError("disk failure")
    monkeypatch.setattr(preference_store, "write_models_preference", fail)
    with pytest.raises(ModelSettingsError):
        save_model_settings(config, {"models": [entry], "credentials": {"SETUP_TEST_KEY": "synthetic-new-key"}})
    assert (home / ".env").read_bytes() == before
    assert not (home / "config.yaml").exists()
    assert config.models[0].api_key == "old-synthetic-key"
    import os
    assert os.environ["SETUP_TEST_KEY"] == "old-synthetic-key"


def test_gateway_reaches_runtime_even_when_required_key_is_deleted(setup_workspace, monkeypatch):
    from backend.app.gateway import app as gateway
    monkeypatch.delenv("SETUP_TEST_KEY")
    @asynccontextmanager
    async def runtime(app, config):
        assert settings_snapshot(config)["setup_required"]
        raise RuntimeError("runtime-reached")
        yield
    monkeypatch.setattr(gateway, "langgraph_runtime", runtime)
    async def scenario():
        async with gateway.lifespan(FastAPI()):
            pass
    with pytest.raises(RuntimeError, match="runtime-reached"):
        asyncio.run(scenario())


def test_non_model_configuration_errors_still_fail(setup_workspace):
    path, entry = setup_workspace
    (path / "config.yaml").write_text(yaml.safe_dump({"models": [entry], "stream_bridge": {"queue_maxsize": "bad"}}), encoding="utf-8")
    with pytest.raises(ValidationError):
        get_app_config("config.yaml", recover_models=True)
