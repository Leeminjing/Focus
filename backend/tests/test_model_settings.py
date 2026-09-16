"""桌面模型设置域（读模型、校验、保存、就地生效、连通性、引用查询、接口确认流）的单测。

输入为临时 `.focus` 家目录 + 临时 cwd 下的 config.yaml；输出为设置快照、写入结果与接口响应。
用例锁定：完整读模型且不含凭据值、来源标注、写入前校验不落盘、只写用户改过的条目、
保存即就地生效、删除需确认、默认解析失败可解释。
"""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.desktop.model_settings import (
    CONFIG_FILE_NAME,
    ModelSettingsError,
    catalog_snapshot,
    probe_model_connection,
    referencing_models,
    save_model_settings,
    settings_snapshot,
    validate_catalog,
)
from backend.app.desktop.model_settings_routes import model_settings_router
from focus.config.app_config import DEFAULT_MODEL_ENV_VAR, build_app_config

_GLOBAL_ENV = "FOCUS_GLOBAL_HOME"
_KEY_VAR = "FOCUS_TEST_MODEL_KEY"


@pytest.fixture(autouse=True)
def _clear_default_model_override(monkeypatch):
    """测试 MUST NOT 依赖宿主的 FOCUS_MODEL。

    它是最高优先级的默认模型来源，宿主（或 CI）一旦设置，所有「声明的默认生效」断言都会失真——
    这不是代码缺陷而是测试的隐式环境依赖，必须在本模块内显式清掉。
    """
    monkeypatch.delenv(DEFAULT_MODEL_ENV_VAR, raising=False)

_FILE_CONFIG = """\
# 发行自带的默认层：程序不该改写这份文件。
stream_bridge:
  type: memory

models:
  - name: alpha
    display_name: Alpha
    use: focus.models.deepseek:DeepSeekChatOpenAI
    model: alpha
    api_key: $FOCUS_TEST_MODEL_KEY
    base_url: https://api.example.com
    context_window: 8192
    curation_output_method: prompt_json
    curation_default: true
    default: true
  - name: beta
    display_name: Beta
    use: focus.models.deepseek:DeepSeekChatOpenAI
    model: beta
    api_key: $FOCUS_TEST_MODEL_KEY
    base_url: https://api.example.com
    context_window: 4096
"""


def _entry(name: str, **overrides) -> dict:
    entry = {
        "name": name,
        "display_name": name.title(),
        "use": "focus.models.deepseek:DeepSeekChatOpenAI",
        "model": name,
        "api_key": f"${_KEY_VAR}",
        "base_url": "https://api.example.com",
        "context_window": 8192,
        "curation_output_method": None,
        "curation_max_output_tokens": 8192,
        "curation_default": False,
        "default": False,
        "supports_image_input": False,
    }
    entry.update(overrides)
    return entry


def _file_catalog() -> list[dict]:
    return [
        _entry(
            "alpha",
            display_name="Alpha",
            curation_output_method="prompt_json",
            curation_default=True,
            default=True,
        ),
        _entry("beta", display_name="Beta", context_window=4096),
    ]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """临时 cwd（文件层）+ 临时 ~/.focus（用户偏好层）+ 已设置的凭据变量。"""
    home = tmp_path / ".focus"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(_GLOBAL_ENV, str(home))
    monkeypatch.setenv(_KEY_VAR, "existing-secret")
    monkeypatch.chdir(tmp_path)
    (tmp_path / CONFIG_FILE_NAME).write_text(_FILE_CONFIG, encoding="utf-8")
    return tmp_path


def _app_config(catalog: list[dict] | None = None):
    file_map = {"models": catalog if catalog is not None else _file_catalog()}
    return build_app_config(file_map, {})


def _preference_document(workspace: Path) -> dict:
    path = workspace / ".focus" / CONFIG_FILE_NAME
    if not path.is_file():
        return {}
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write_preference(workspace: Path, models: list[dict], removed: list[str]) -> None:
    """把用户偏好层写到磁盘，使读模型的来源标注基于与加载器一致的两层输入。"""
    import yaml

    from focus.config.preference_store import preference_config_path

    path = preference_config_path(CONFIG_FILE_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {"models": models, "removed_models": removed}, allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )


def _load_config(workspace: Path):
    """按加载器的方式从磁盘两层构造配置。"""
    from focus.config.layered import load_layered_maps

    file_map, preference_map = load_layered_maps(CONFIG_FILE_NAME, CONFIG_FILE_NAME)
    return file_map, build_app_config(file_map, preference_map)


# --- 读模型 ---------------------------------------------------------------


def test_catalog_snapshot_exposes_every_editable_field(workspace):
    config = _app_config()

    entries = catalog_snapshot(config)

    assert len(entries) == 2
    for entry in entries:
        for field in (
            "name",
            "display_name",
            "use",
            "model",
            "api_key",
            "base_url",
            "context_window",
            "curation_output_method",
            "curation_max_output_tokens",
            "curation_default",
            "default",
            "supports_image_input",
        ):
            assert field in entry


def test_catalog_snapshot_never_exposes_the_secret(workspace):
    config = _app_config()

    entries = catalog_snapshot(config)
    rendered = json.dumps(entries, ensure_ascii=False)

    assert "existing-secret" not in rendered
    assert entries[0]["api_key"] == f"${_KEY_VAR}"
    assert entries[0]["api_key_variable"] == _KEY_VAR
    assert entries[0]["api_key_set"] is True
    assert entries[0]["api_key_literal"] is False


def test_catalog_snapshot_reports_credential_state(workspace, monkeypatch):
    monkeypatch.delenv(_KEY_VAR, raising=False)
    config = _app_config()

    entries = catalog_snapshot(config)

    assert entries[0]["api_key_set"] is False


def test_catalog_snapshot_never_echoes_a_literal_key(workspace):
    import yaml

    literal = "sk-literal-must-not-leak"
    entry = _entry(
        "alpha",
        api_key=literal,
        default=True,
        curation_default=True,
        curation_output_method="prompt_json",
    )
    (workspace / CONFIG_FILE_NAME).write_text(
        yaml.safe_dump({"models": [entry]}, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    config = build_app_config({"models": [entry]}, {})

    entries = catalog_snapshot(config)

    assert literal not in json.dumps(entries, ensure_ascii=False)
    assert entries[0]["api_key"] == ""
    assert entries[0]["api_key_variable"] is None
    assert entries[0]["api_key_literal"] is True
    assert entries[0]["api_key_set"] is True


def test_catalog_snapshot_labels_sources(workspace):
    _write_preference(
        workspace,
        [
            _entry(
                "alpha",
                display_name="Alpha",
                curation_output_method="prompt_json",
                curation_default=True,
                default=False,
                context_window=16384,
            ),
            _entry("gamma", default=True),
        ],
        ["beta"],
    )
    _, config = _load_config(workspace)

    entries = {entry["name"]: entry for entry in catalog_snapshot(config)}

    assert entries["alpha"]["source"] == "user_modified"
    assert entries["gamma"]["source"] == "user_added"


def test_settings_snapshot_explains_a_broken_default(workspace, monkeypatch):
    monkeypatch.setenv("FOCUS_MODEL", "nope")
    config = _app_config()

    snapshot = settings_snapshot(config)

    assert snapshot["default_model_name"] is None
    assert "nope" in snapshot["default_error"]
    assert "alpha" in snapshot["default_error"]
    assert {adapter["use"] for adapter in snapshot["adapters"]} >= {
        "focus.models.deepseek:DeepSeekChatOpenAI"
    }


def test_settings_snapshot_reports_effective_defaults(workspace):
    snapshot = settings_snapshot(_app_config())

    assert snapshot["default_model_name"] == "alpha"
    assert snapshot["curation_default_model_name"] == "alpha"
    assert snapshot["removed_models"] == []
    assert snapshot["default_model_override"] is None


def test_settings_snapshot_exposes_the_env_override(workspace, monkeypatch):
    """FOCUS_MODEL 压过面板里的默认选择时必须可见，否则界面显示的默认徽标会骗人。"""
    monkeypatch.setenv(DEFAULT_MODEL_ENV_VAR, "beta")

    snapshot = settings_snapshot(_app_config())

    assert snapshot["default_model_override"] == "beta"
    assert snapshot["default_model_name"] == "beta"  # 覆盖确实生效了


# --- 校验 -----------------------------------------------------------------


def test_validate_catalog_accepts_the_shipped_catalog():
    validate_catalog(_file_catalog())


@pytest.mark.parametrize(
    "mutate, field",
    [
        (lambda c: c[0].pop("base_url"), "models[0].base_url"),
        (lambda c: c[0].update({"use": "evil.module:Backdoor"}), "models[0].use"),
        (lambda c: c[0].update({"base_url": "ftp://api.example.com"}), "models[0].base_url"),
        (lambda c: c[0].update({"context_window": 0}), "models[0].context_window"),
        (lambda c: c[0].update({"context_window": "big"}), "models[0].context_window"),
        (lambda c: c[0].update({"curation_output_method": "yaml"}), "models[0].curation_output_method"),
        (lambda c: c[0].update({"curation_output_method": None}), "models[0].curation_output_method"),
        (lambda c: c[0].update({"default": False}), "default"),
        (lambda c: c[0].update({"curation_default": False}), "curation_default"),
        (lambda c: c[0].update({"mystery": 1}), "models[0].mystery"),
        (lambda c: c.append(_entry("alpha")), "models[2].name"),
    ],
)
def test_validate_catalog_rejects(mutate, field):
    catalog = _file_catalog()
    mutate(catalog)

    with pytest.raises(ModelSettingsError) as excinfo:
        validate_catalog(catalog)

    assert field in excinfo.value.errors


def test_validate_catalog_rejects_an_empty_catalog():
    with pytest.raises(ModelSettingsError) as excinfo:
        validate_catalog([])

    assert "models" in excinfo.value.errors


def test_validate_catalog_rejects_two_defaults():
    catalog = _file_catalog()
    catalog[0]["default"] = True
    catalog[1]["default"] = True

    with pytest.raises(ModelSettingsError) as excinfo:
        validate_catalog(catalog)

    assert "default" in excinfo.value.errors


# --- 保存与就地生效 -------------------------------------------------------


def test_save_writes_only_the_changed_entries(workspace):
    config = _app_config()
    catalog = _file_catalog()
    catalog[0]["context_window"] = 16384
    catalog.append(_entry("gamma"))

    snapshot = save_model_settings(config, {"models": catalog})

    document = _preference_document(workspace)
    assert [entry["name"] for entry in document["models"]] == ["alpha", "gamma"]
    assert document["models"][0]["context_window"] == 16384
    assert document["removed_models"] == []
    assert [entry["source"] for entry in snapshot["models"]] == [
        "user_modified",
        "shipped",
        "user_added",
    ]


def test_save_leaves_the_file_layer_untouched(workspace):
    config = _app_config()
    before = (workspace / CONFIG_FILE_NAME).read_text(encoding="utf-8")
    catalog = _file_catalog()
    catalog.append(_entry("gamma"))

    save_model_settings(config, {"models": catalog})

    assert (workspace / CONFIG_FILE_NAME).read_text(encoding="utf-8") == before


def test_save_applies_in_place_to_the_held_object(workspace):
    config = _app_config()
    held = config
    catalog = _file_catalog()
    catalog.append(_entry("gamma"))

    save_model_settings(config, {"models": catalog})

    assert held is config
    assert [model.name for model in held.models] == ["alpha", "beta", "gamma"]


def test_save_switches_the_default_and_clears_the_file_layer_marker(workspace):
    config = _app_config()
    catalog = _file_catalog()
    catalog[0]["default"] = False
    catalog[1]["default"] = True

    save_model_settings(config, {"models": catalog})

    assert config.resolve_default_model_name() == "beta"
    assert [model.name for model in config.models if model.default] == ["beta"]


def test_save_records_removals_and_keeps_them_after_the_file_layer_grows(workspace):
    config = _app_config()
    catalog = [entry for entry in _file_catalog() if entry["name"] != "beta"]

    snapshot = save_model_settings(config, {"models": catalog})

    document = _preference_document(workspace)
    assert document["removed_models"] == ["beta"]
    assert [model.name for model in config.models] == ["alpha"]
    assert snapshot["removed_models"] == ["beta"]


def test_save_writes_the_credential_and_reports_it_set(workspace):
    config = _app_config()
    catalog = _file_catalog()

    snapshot = save_model_settings(
        config, {"models": catalog, "credentials": {_KEY_VAR: "rotated-secret"}}
    )

    env_text = (workspace / ".focus" / ".env").read_text(encoding="utf-8")
    assert "rotated-secret" in env_text
    assert os.environ[_KEY_VAR] == "rotated-secret"
    assert all(entry["api_key_set"] for entry in snapshot["models"])
    assert "rotated-secret" not in json.dumps(snapshot, ensure_ascii=False)


def test_save_rejects_two_defaults_without_touching_anything(workspace):
    config = _app_config()
    catalog = _file_catalog()
    catalog[1]["default"] = True
    before_models = [model.name for model in config.models]

    with pytest.raises(ModelSettingsError) as excinfo:
        save_model_settings(config, {"models": catalog})

    assert "default" in excinfo.value.errors
    assert not (workspace / ".focus" / CONFIG_FILE_NAME).exists()
    assert [model.name for model in config.models] == before_models


def test_save_rejects_a_missing_credential_without_writing(workspace, monkeypatch):
    monkeypatch.delenv(_KEY_VAR, raising=False)
    config = _app_config()

    with pytest.raises(ModelSettingsError) as excinfo:
        save_model_settings(config, {"models": _file_catalog()})

    assert any("凭据来源变量未设置" in message for message in excinfo.value.errors.values())
    assert not (workspace / ".focus" / ".env").exists()
    assert not (workspace / ".focus" / CONFIG_FILE_NAME).exists()


def test_save_rejects_unknown_field_without_writing(workspace):
    config = _app_config()
    catalog = _file_catalog()
    catalog[0]["shell"] = "rm -rf /"

    with pytest.raises(ModelSettingsError):
        save_model_settings(config, {"models": catalog})

    assert not (workspace / ".focus" / CONFIG_FILE_NAME).exists()


def test_save_keeps_the_preference_file_comments(workspace):
    preference = workspace / ".focus" / CONFIG_FILE_NAME
    preference.write_text("# 我自己的说明\nstream_bridge:\n  type: memory\n", encoding="utf-8")
    config = _app_config()
    catalog = _file_catalog()
    catalog.append(_entry("gamma"))

    save_model_settings(config, {"models": catalog})

    text = preference.read_text(encoding="utf-8")
    assert "# 我自己的说明" in text
    assert "queue_maxsize" not in text
    assert "gamma" in text


def test_save_is_idempotent_on_repeat(workspace):
    config = _app_config()
    catalog = _file_catalog()
    catalog[0]["context_window"] = 16384

    first = save_model_settings(config, {"models": catalog})
    second = save_model_settings(config, {"models": catalog})

    assert first["models"] == second["models"]


# --- 连通性测试 -----------------------------------------------------------


def _patch_model(monkeypatch, result=None, error=None):
    import focus.models as models_module

    captured: dict = {"invoked": False}

    class _Stub:
        async def ainvoke(self, _prompt):
            captured["invoked"] = True
            if error is not None:
                raise error
            return result

    def fake_create(name=None, *, app_config=None, **kwargs):
        captured["name"] = name
        captured["api_key"] = app_config.get_model(name).api_key
        captured["kwargs"] = kwargs
        return _Stub()

    monkeypatch.setattr(models_module, "create_chat_model", fake_create)
    return captured


def test_probe_connection_reports_success(workspace, monkeypatch):
    captured = _patch_model(monkeypatch)

    result = asyncio.run(probe_model_connection(_entry("alpha"), api_key_value="pending-secret"))

    assert result == {"ok": True, "reason": "连接成功"}
    assert captured["invoked"] is True  # 探活必须真的发一次调用
    assert captured["api_key"] == "pending-secret"
    assert captured["kwargs"]["max_tokens"] == 1
    assert os.environ.get("pending-secret") is None


def test_probe_connection_uses_the_stored_credential_when_no_value_given(workspace, monkeypatch):
    captured = _patch_model(monkeypatch)

    result = asyncio.run(probe_model_connection(_entry("alpha")))

    assert result["ok"] is True
    assert captured["api_key"] == "existing-secret"


def test_probe_connection_asks_for_a_key_when_there_is_none(workspace, monkeypatch):
    _patch_model(monkeypatch)
    entry = _entry("alpha")
    entry["api_key"] = ""

    with pytest.raises(ModelSettingsError) as excinfo:
        asyncio.run(probe_model_connection(entry))

    assert "models[0].api_key" in excinfo.value.errors


def test_probe_connection_maps_auth_failure_and_redacts_the_value(workspace, monkeypatch):
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.example.com/chat/completions")
    error = openai.AuthenticationError(
        "bad key pending-secret", response=httpx.Response(401, request=request), body=None
    )
    _patch_model(monkeypatch, error=error)

    result = asyncio.run(probe_model_connection(_entry("alpha"), api_key_value="pending-secret"))

    assert result["ok"] is False
    assert "401" in result["reason"] or "凭据" in result["reason"]
    assert "pending-secret" not in result["reason"]


def test_probe_connection_maps_timeout(workspace, monkeypatch):
    import httpx
    import openai

    _patch_model(monkeypatch, error=openai.APITimeoutError(request=httpx.Request("POST", "https://x")))

    result = asyncio.run(probe_model_connection(_entry("alpha")))

    assert result["ok"] is False
    assert "超时" in result["reason"]


def test_probe_connection_surfaces_the_provider_message(workspace, monkeypatch):
    """「模型不存在」在不同兼容层既可能是 404 也可能是 400 + 原文，必须把原文带出来。"""
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.example.com/chat/completions")
    response = httpx.Response(
        400,
        request=request,
        json={"error": {"message": "Model Not Exist", "type": "invalid_request_error"}},
    )
    error = openai.BadRequestError(
        "bad request", response=response, body={"error": {"message": "Model Not Exist"}}
    )
    _patch_model(monkeypatch, error=error)

    result = asyncio.run(probe_model_connection(_entry("alpha")))

    assert result["ok"] is False
    assert "400" in result["reason"]
    assert "Model Not Exist" in result["reason"]


def test_probe_connection_redacts_the_provider_message_too(workspace, monkeypatch):
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.example.com/chat/completions")
    response = httpx.Response(
        401,
        request=request,
        json={"error": {"message": "invalid api key pending-secret"}},
    )
    error = openai.AuthenticationError(
        "unauthorized",
        response=response,
        body={"error": {"message": "invalid api key pending-secret"}},
    )
    _patch_model(monkeypatch, error=error)

    result = asyncio.run(probe_model_connection(_entry("alpha"), api_key_value="pending-secret"))

    assert "pending-secret" not in result["reason"]
    assert "***" in result["reason"]


def test_probe_connection_rejects_an_invalid_entry(workspace):
    entry = _entry("alpha")
    entry["base_url"] = "not-a-url"

    with pytest.raises(ModelSettingsError):
        asyncio.run(probe_model_connection(entry))


# --- 保存路径不联网 / 条目缺失的可读失败 / 工具面边界 ----------------------


def test_save_never_performs_a_model_round_trip(workspace, monkeypatch):
    """保存只构造客户端、不发起模型调用；联网验证必须由人显式点击。"""
    import focus.models as models_module

    class _Stub:
        def invoke(self, *_args, **_kwargs):
            raise AssertionError("保存路径不得发起模型调用")

        async def ainvoke(self, *_args, **_kwargs):
            raise AssertionError("保存路径不得发起模型调用")

    calls: list = []

    def fake_create(*args, **kwargs):
        calls.append(args)
        return _Stub()

    monkeypatch.setattr(models_module, "create_chat_model", fake_create)
    config = _app_config()
    catalog = _file_catalog()
    catalog.append(_entry("gamma"))

    snapshot = save_model_settings(config, {"models": catalog})

    assert len(calls) == 1  # 只做装配自检（构造），没有任何一次 invoke/ainvoke
    assert "gamma" in [entry["name"] for entry in snapshot["models"]]


def test_missing_entry_yields_a_readable_422_without_fallback(workspace):
    """条目被删除后，运行发起前必须给可读错误，而不是 KeyError 或静默改用别的条目。"""
    from fastapi import HTTPException

    from backend.app.desktop.service import DesktopService

    service = DesktopService.__new__(DesktopService)
    service.app_config = _app_config()

    with pytest.raises(HTTPException) as excinfo:
        service._require_model("已删除的条目")

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "model_entry_missing"
    assert "已删除的条目" in excinfo.value.detail["message"]
    assert "alpha" in excinfo.value.detail["message"]  # 顺带告诉用户有哪些可用条目

    # 存在时正常返回，不做任何替换
    assert service._require_model("alpha").name == "alpha"


def test_undeclared_default_yields_a_readable_422(workspace):
    from fastapi import HTTPException

    from backend.app.desktop.service import DesktopService

    service = DesktopService.__new__(DesktopService)
    service.app_config = build_app_config({"models": [_entry("alpha"), _entry("beta")]}, {})

    with pytest.raises(HTTPException) as excinfo:
        service._require_model(None)

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "model_entry_missing"
    assert "alpha" in excinfo.value.detail["message"]


def test_settings_write_path_is_not_reachable_from_the_agent_tool_surface():
    """设置写入口 MUST NOT 注册为 Agent 工具：工具包内不得引用该模块。"""
    import focus.tools as tools_package

    root = Path(tools_package.__file__).parent
    offenders = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "desktop.model_settings" in path.read_text(encoding="utf-8")
    )

    assert offenders == []


# --- 接口层：删除确认与字段级错误 -----------------------------------------


def _client(app_config, session_factory=None):
    app = FastAPI()
    app.include_router(model_settings_router)
    app.state.desktop_service = SimpleNamespace(
        app_config=app_config, session_factory=session_factory
    )
    return TestClient(app)


def test_route_rejects_an_invalid_catalog_with_field_errors(workspace):
    client = _client(_app_config())
    catalog = _file_catalog()
    catalog[0]["base_url"] = "nope"

    response = client.put("/desktop/api/settings/models", json={"models": catalog})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "model_settings_invalid"
    assert "models[0].base_url" in detail["errors"]


def test_route_requires_confirmation_before_deleting_a_referenced_entry(workspace, monkeypatch):
    import backend.app.desktop.model_settings_routes as routes

    async def fake_references(session_factory, names):
        return {"beta": [{"kind": "run", "run_id": "r1", "task_id": "t1"}]}

    monkeypatch.setattr(routes, "referencing_models", fake_references)
    client = _client(_app_config())
    catalog = [entry for entry in _file_catalog() if entry["name"] != "beta"]

    blocked = client.put("/desktop/api/settings/models", json={"models": catalog})

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "model_referenced"
    assert "beta" in blocked.json()["detail"]["references"]

    confirmed = client.put(
        "/desktop/api/settings/models",
        json={"models": catalog, "acknowledge_references": True},
    )

    assert confirmed.status_code == 200
    assert [entry["name"] for entry in confirmed.json()["models"]] == ["alpha"]


def test_route_lists_references(workspace, monkeypatch):
    import backend.app.desktop.model_settings_routes as routes

    async def fake_references(session_factory, names):
        return {name: [{"kind": "task", "task_id": "t1"}] for name in names}

    monkeypatch.setattr(routes, "referencing_models", fake_references)
    client = _client(_app_config())

    response = client.post("/desktop/api/settings/models/references", json={"names": ["alpha"]})

    assert response.status_code == 200
    assert response.json()["references"]["alpha"][0]["task_id"] == "t1"


def test_route_reports_missing_service_as_unavailable(workspace):
    app = FastAPI()
    app.include_router(model_settings_router)
    client = TestClient(app)

    response = client.get("/desktop/api/settings/models")

    assert response.status_code == 503


def test_route_returns_the_snapshot(workspace):
    client = _client(_app_config())

    response = client.get("/desktop/api/settings/models")

    assert response.status_code == 200
    assert response.json()["default_model_name"] == "alpha"


# --- 引用查询（真实数据库） -----------------------------------------------


def _local_session_factory():
    """建一个**本用例私有**的引擎与 session factory。

    刻意不复用 `focus.persistence.engine` 的进程级单例：那里的 engine 绑定在创建它的事件
    循环上，而本用例用 `asyncio.run` 跑自己的循环。共用单例会让后续模块（例如经由 TestClient
    启动 Gateway lifespan 的接口测试）拿到绑在已关闭循环上的连接池，报
    "got Future attached to a different loop"。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(os.environ["FOCUS_DATABASE_URL"])
    return async_sessionmaker(engine, expire_on_commit=False), engine


def test_referencing_models_finds_tasks_and_runs(isolated_postgres_database):
    from sqlalchemy import delete

    from backend.app.desktop.models import (
        MAIN_RUN_EQUIPMENT_KEY,
        DesktopRun,
        DesktopThread,
        DesktopWorkspace,
    )

    task_id = "task-modelsettings-ref01"
    workspace_id = "ws-modelsettings-ref01"
    run_id = "run-modelsettings-ref01"

    async def scenario():
        factory, engine = _local_session_factory()
        try:
            async with factory() as session:
                await session.execute(delete(DesktopRun).where(DesktopRun.run_id == run_id))
                await session.execute(delete(DesktopThread).where(DesktopThread.task_id == task_id))
                await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id))
                session.add(DesktopWorkspace(workspace_id=workspace_id, path="C:/ws-ref", display_name="ref"))
                await session.flush()
                session.add(
                    DesktopThread(
                        task_id=task_id,
                        workspace_id=workspace_id,
                        thread_id="thread-ref-01",
                        title="reference probe",
                        ui_state={MAIN_RUN_EQUIPMENT_KEY: {"model_name": "alpha"}},
                    )
                )
                await session.flush()
                session.add(
                    DesktopRun(
                        run_id=run_id,
                        task_id=task_id,
                        agent_id="main",
                        kind="main",
                        status="success",
                        model_name="alpha",
                    )
                )
                await session.commit()

            references = await referencing_models(factory, ["alpha", "nothing"])

            async with factory() as session:
                await session.execute(delete(DesktopRun).where(DesktopRun.run_id == run_id))
                await session.execute(delete(DesktopThread).where(DesktopThread.task_id == task_id))
                await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id))
                await session.commit()
            return references
        finally:
            await engine.dispose()

    references = asyncio.run(scenario())

    kinds = {item["kind"] for item in references["alpha"]}
    assert kinds == {"task", "run"}
    assert "nothing" not in references
