"""配置对象的构造、就地生效与默认模型解析失败信息的单测。

输入为两层原始 map 与环境变量；输出为已校验的 AppConfig、就地更新后的既有对象与错误信息。
用例锁定：用户偏好层优先、就地更新保持对象身份、校验由新对象承担、失败信息自解释。
"""

import os

import pytest
from pydantic import ValidationError

from focus.config.app_config import (
    DEFAULT_MODEL_ENV_VAR,
    AppConfig,
    apply_app_config,
    build_app_config,
)


def _entry(name: str, **overrides) -> dict:
    entry = {
        "name": name,
        "display_name": name.upper(),
        "use": "focus.models.deepseek:DeepSeekChatOpenAI",
        "model": name,
        "api_key": "$OPENAI_API_KEY",
        "base_url": "https://api.example.com",
        "context_window": 8192,
    }
    entry.update(overrides)
    return entry


@pytest.fixture(autouse=True)
def _clear_override(monkeypatch):
    monkeypatch.delenv(DEFAULT_MODEL_ENV_VAR, raising=False)


def test_preference_layer_decides_the_default_entry():
    file_map = {"models": [_entry("shipped", default=True)]}
    preference_map = {"models": [_entry("mine", default=True)]}

    config = build_app_config(file_map, preference_map)

    assert config.resolve_default_model_name() == "mine"


def test_preference_layer_keeps_file_entries():
    file_map = {"models": [_entry("shipped", default=True)]}
    preference_map = {"models": [_entry("mine")]}

    config = build_app_config(file_map, preference_map)

    assert [model.name for model in config.models] == ["shipped", "mine"]
    assert config.resolve_default_model_name() == "shipped"


def test_removed_models_are_visible_on_the_config():
    config = build_app_config({"models": [_entry("a")]}, {"models": [], "removed_models": ["a"]})

    assert config.models == []
    assert config.removed_models == ["a"]


def test_apply_keeps_object_identity_and_updates_earlier_references():
    target = build_app_config({"models": [_entry("a", default=True)]}, {})
    held = target  # 调用方（桌面服务 / 巡检 / 策展）按引用持有的那个对象

    fresh = build_app_config({"models": [_entry("a")]}, {"models": [_entry("b", default=True)]})
    returned = apply_app_config(target, fresh)

    assert returned is target
    assert held is target
    assert [model.name for model in held.models] == ["a", "b"]
    assert held.resolve_default_model_name() == "b"


def test_apply_replaces_removed_models_too():
    target = build_app_config({"models": [_entry("a")]}, {})
    fresh = build_app_config({"models": [_entry("a")]}, {"removed_models": ["a"]})

    apply_app_config(target, fresh)

    assert target.removed_models == ["a"]
    assert target.models == []


def test_validation_is_carried_by_the_fresh_object():
    target = build_app_config({"models": [_entry("a", default=True)]}, {})

    with pytest.raises(ValidationError):
        build_app_config(
            {"models": [_entry("a")]}, {"models": [_entry("x", default=True), _entry("y", default=True)]}
        )

    # 构造失败发生在写入之前，既有对象保持原样
    assert [model.name for model in target.models] == ["a"]


def test_missing_models_key_fails_loudly():
    with pytest.raises(ValidationError):
        build_app_config({"commitment": {"enabled": True}}, {})


def test_only_documented_env_overrides_apply(monkeypatch):
    """没有文档化覆写键的配置项 MUST NOT 被同名环境变量改写：不存在通用 env 覆盖层。"""
    monkeypatch.setenv("COMMITMENT_ENABLED", "true")
    monkeypatch.setenv("COMPRESSION_ENABLED", "true")
    monkeypatch.setenv("MODELS", "[]")
    file_map = {
        "models": [_entry("alpha", default=True)],
        "commitment": {"enabled": False},
        "compression": {"enabled": False},
    }

    config = build_app_config(file_map, {})

    assert config.commitment.enabled is False
    assert config.compression.enabled is False
    assert [model.name for model in config.models] == ["alpha"]


def test_incomplete_preference_entry_is_rejected_without_backfill():
    """用户偏好层的条目必须完整声明：缺字段要在加载期报出来，不得用文件层同名字段补全。"""
    file_map = {"models": [_entry("alpha", default=True)]}
    preference_map = {"models": [{"name": "alpha", "context_window": 4096}]}

    with pytest.raises(ValidationError) as excinfo:
        build_app_config(file_map, preference_map)

    message = str(excinfo.value)
    # 缺失字段被指出来；若回退到文件层补全，这里根本不会抛错
    assert "display_name" in message or "base_url" in message or "api_key" in message


def test_env_override_names_all_available_entries(monkeypatch):
    monkeypatch.setenv(DEFAULT_MODEL_ENV_VAR, "DeepSeek-V4.1-Flash")
    config = build_app_config({"models": [_entry("alpha", default=True), _entry("beta")]}, {})

    with pytest.raises(ValueError) as excinfo:
        config.resolve_default_model_name()

    message = str(excinfo.value)
    assert "DeepSeek-V4.1-Flash" in message
    assert "alpha" in message and "beta" in message
    assert "桌面设置" in message


def test_missing_default_names_all_available_entries():
    config = build_app_config({"models": [_entry("alpha"), _entry("beta")]}, {})

    with pytest.raises(ValueError) as excinfo:
        config.resolve_default_model_name()

    message = str(excinfo.value)
    assert "alpha" in message and "beta" in message
    assert "桌面设置" in message


def test_get_model_reports_available_entries():
    config = build_app_config({"models": [_entry("alpha")]}, {})

    with pytest.raises(KeyError) as excinfo:
        config.get_model("nope")

    assert "alpha" in str(excinfo.value)


def test_resolution_does_not_fall_back_to_list_position(monkeypatch):
    config = build_app_config({"models": [_entry("alpha"), _entry("beta")]}, {})

    with pytest.raises(ValueError):
        config.resolve_default_model_name()

    monkeypatch.setenv(DEFAULT_MODEL_ENV_VAR, " beta ")  # 允许两侧空白，但必须是已存在条目
    assert config.resolve_default_model_name() == "beta"


def test_env_override_wins_over_declared_default(monkeypatch):
    config = build_app_config({"models": [_entry("alpha", default=True), _entry("beta")]}, {})
    monkeypatch.setenv(DEFAULT_MODEL_ENV_VAR, "beta")

    assert config.resolve_default_model_name() == "beta"


def test_app_config_is_importable_without_extra_fields():
    config = AppConfig.model_validate({"models": [dict(_entry("a"))]})

    assert config.removed_models == []
    assert os.environ.get(DEFAULT_MODEL_ENV_VAR) is None
