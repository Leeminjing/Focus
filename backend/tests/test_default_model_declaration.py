"""默认模型显式声明与解析的契约测试。

覆盖：声明唯一性校验、显式声明取代列表位置约定、环境变量覆盖，以及各类失败信息。
"""

import pytest

from focus.config.app_config import _load_layered_config

_MODEL_ENV = "FOCUS_MODEL"


@pytest.fixture(autouse=True)
def _clear_model_override(monkeypatch):
    monkeypatch.delenv(_MODEL_ENV, raising=False)


def _entry(name: str, *, default: bool = False, api_key: str = "test-key") -> str:
    lines = [
        f"  - name: {name}",
        f"    display_name: {name}",
        "    use: focus.models.deepseek:DeepSeekChatOpenAI",
        f"    model: {name}",
        f"    api_key: {api_key}",
        "    base_url: https://example.test",
    ]
    if default:
        lines.append("    default: true")
    return "\n".join(lines) + "\n"


def _load(tmp_path, monkeypatch, models_yaml: str):
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / ".focus"))
    config = tmp_path / "config.yaml"
    config.write_text("models:\n" + models_yaml, encoding="utf-8")
    return _load_layered_config(str(config))


# === 声明唯一性 ===


def test_single_declaration_resolves(tmp_path, monkeypatch):
    app_config = _load(tmp_path, monkeypatch, _entry("alpha", default=True) + _entry("beta"))
    assert app_config.resolve_default_model_name() == "alpha"


def test_multiple_declarations_are_rejected(tmp_path, monkeypatch):
    with pytest.raises(Exception) as excinfo:
        _load(tmp_path, monkeypatch, _entry("alpha", default=True) + _entry("beta", default=True))
    assert "只能声明一个默认模型" in str(excinfo.value)


def test_no_declaration_still_loads(tmp_path, monkeypatch):
    """零声明不阻塞配置加载；失败推迟到解析默认模型时（与 curation_default 语义一致）。"""
    app_config = _load(tmp_path, monkeypatch, _entry("alpha") + _entry("beta"))
    assert [model.name for model in app_config.models] == ["alpha", "beta"]


def test_no_declaration_resolution_reports_guidance(tmp_path, monkeypatch):
    app_config = _load(tmp_path, monkeypatch, _entry("alpha") + _entry("beta"))
    with pytest.raises(ValueError) as excinfo:
        app_config.resolve_default_model_name()
    message = str(excinfo.value)
    assert "未声明默认模型" in message
    assert _MODEL_ENV in message


# === 不再依赖列表位置 ===


def test_list_order_does_not_change_default(tmp_path, monkeypatch):
    """重排条目顺序不得改变默认模型（原先 models[0] 约定下会改变）。"""
    app_config = _load(tmp_path, monkeypatch, _entry("beta") + _entry("alpha", default=True))
    assert app_config.resolve_default_model_name() == "alpha"


def test_default_is_not_the_first_entry(tmp_path, monkeypatch):
    app_config = _load(tmp_path, monkeypatch, _entry("alpha") + _entry("beta", default=True))
    assert app_config.models[0].name == "alpha"
    assert app_config.resolve_default_model_name() == "beta"


# === 环境变量覆盖 ===


def test_env_override_wins_over_declaration(tmp_path, monkeypatch):
    monkeypatch.setenv(_MODEL_ENV, "beta")
    app_config = _load(tmp_path, monkeypatch, _entry("alpha", default=True) + _entry("beta"))
    assert app_config.resolve_default_model_name() == "beta"


def test_env_override_works_without_declaration(tmp_path, monkeypatch):
    monkeypatch.setenv(_MODEL_ENV, "beta")
    app_config = _load(tmp_path, monkeypatch, _entry("alpha") + _entry("beta"))
    assert app_config.resolve_default_model_name() == "beta"


def test_env_override_naming_unknown_entry_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv(_MODEL_ENV, "no-such-model")
    app_config = _load(tmp_path, monkeypatch, _entry("alpha", default=True))
    with pytest.raises(ValueError) as excinfo:
        app_config.resolve_default_model_name()
    assert "no-such-model" in str(excinfo.value)


def test_blank_env_override_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv(_MODEL_ENV, "   ")
    app_config = _load(tmp_path, monkeypatch, _entry("alpha", default=True))
    assert app_config.resolve_default_model_name() == "alpha"


# === 装配默认模型（启动自检所依赖的路径）===


def test_assembly_reports_missing_credential_with_model_name(tmp_path, monkeypatch):
    """装配默认模型时，必需凭据缺失的错误须同时指出变量名与模型名。"""
    monkeypatch.delenv("FOCUS_TEST_MODEL_KEY", raising=False)
    app_config = _load(
        tmp_path,
        monkeypatch,
        _entry("alpha", default=True, api_key="$FOCUS_TEST_MODEL_KEY"),
    )

    from focus.models import create_chat_model

    with pytest.raises(ValueError) as excinfo:
        create_chat_model(app_config=app_config)

    message = str(excinfo.value)
    assert "FOCUS_TEST_MODEL_KEY" in message
    assert "alpha" in message


def test_assembly_succeeds_when_credential_present(tmp_path, monkeypatch):
    monkeypatch.setenv("FOCUS_TEST_MODEL_KEY", "resolved-key")
    app_config = _load(
        tmp_path,
        monkeypatch,
        _entry("alpha", default=True, api_key="$FOCUS_TEST_MODEL_KEY"),
    )

    from focus.models import create_chat_model

    model = create_chat_model(app_config=app_config)

    assert model.model_name == "alpha"
