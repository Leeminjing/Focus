"""环境变量引用解析契约测试：加载期软解析、使用期显式失败、非引用字面量不被误判。"""

import os

import pytest

from focus.config.app_config import _load_layered_config
from focus.config.env import (
    match_env_ref,
    resolve_env_var,
    resolve_env_vars,
    require_env_var,
)
from focus.config.layered import load_global_dotenv

_MISSING = "FOCUS_TEST_UNSET_KEY"


@pytest.fixture(autouse=True)
def _ensure_missing_key_unset(monkeypatch):
    monkeypatch.delenv(_MISSING, raising=False)


# === 引用识别 ===


def test_dollar_prefixed_non_reference_is_not_a_reference():
    """`$100` 这类以 $ 开头但非合法标识符的值不得被判为引用。"""
    for literal in ("$100", "$foo-bar", "$", "$1", "$100.00"):
        assert match_env_ref(literal) is None


def test_identifier_forms_are_recognized():
    assert match_env_ref("$OPENAI_API_KEY") == "OPENAI_API_KEY"
    assert match_env_ref("${OPENAI_API_KEY}") == "OPENAI_API_KEY"


def test_non_string_values_are_not_references():
    assert match_env_ref(123) is None
    assert match_env_ref(None) is None
    assert match_env_ref(["$A"]) is None


# === 加载期：软解析，永不抛错 ===


def test_load_phase_keeps_unresolved_reference_as_literal():
    assert resolve_env_vars(f"${_MISSING}") == f"${_MISSING}"


def test_load_phase_does_not_raise_for_dollar_literals():
    # 变更前该值会因 startswith("$") 被判为引用而触发 KeyError
    assert resolve_env_vars("$100") == "$100"


def test_load_phase_resolves_present_reference(monkeypatch):
    monkeypatch.setenv("FOCUS_TEST_PRESENT_KEY", "resolved-value")
    assert resolve_env_vars("$FOCUS_TEST_PRESENT_KEY") == "resolved-value"


def test_load_phase_recurses_through_dict_and_list(monkeypatch):
    monkeypatch.setenv("FOCUS_TEST_PRESENT_KEY", "resolved-value")
    raw = {
        "models": [{"api_key": "$FOCUS_TEST_PRESENT_KEY", "other": f"${_MISSING}"}],
        "prices": ["$100", 42, None],
    }
    assert resolve_env_vars(raw) == {
        "models": [{"api_key": "resolved-value", "other": f"${_MISSING}"}],
        "prices": ["$100", 42, None],
    }


def test_resolve_env_var_returns_none_for_non_reference_and_unset():
    assert resolve_env_var("$100") is None
    assert resolve_env_var(f"${_MISSING}") is None


# === 使用期：显式失败并携带上下文 ===


def test_require_returns_non_reference_unchanged():
    assert require_env_var("$100", context="demo") == "$100"
    assert require_env_var("plain-value", context="demo") == "plain-value"


def test_require_resolves_present_reference(monkeypatch):
    monkeypatch.setenv("FOCUS_TEST_PRESENT_KEY", "resolved-value")
    assert require_env_var("$FOCUS_TEST_PRESENT_KEY", context="demo") == "resolved-value"


def test_require_raises_with_variable_name_and_context():
    with pytest.raises(ValueError) as excinfo:
        require_env_var(f"${_MISSING}", context="模型 'demo-model'")
    message = str(excinfo.value)
    assert _MISSING in message
    assert "demo-model" in message


def test_require_failure_is_value_error_not_key_error():
    """插件装配链以 ValueError 判定依赖缺失并降级，失败类型不得改变。"""
    with pytest.raises(ValueError) as excinfo:
        require_env_var(f"${_MISSING}", context="plugin")
    assert not isinstance(excinfo.value, KeyError)


# === 集成：配置加载不再因引用缺失而失败 ===


def test_app_config_loads_when_reference_is_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / ".focus"))
    config = tmp_path / "config.yaml"
    config.write_text(
        "models:\n"
        "  - name: demo\n"
        "    display_name: Demo\n"
        "    use: focus.models.deepseek:DeepSeekChatOpenAI\n"
        "    model: demo\n"
        f"    api_key: ${_MISSING}\n"
        "    base_url: https://example.test\n",
        encoding="utf-8",
    )

    app_config = _load_layered_config(str(config))

    assert [model.name for model in app_config.models] == ["demo"]
    assert app_config.get_model("demo").api_key == f"${_MISSING}"


def test_app_config_loads_with_dollar_literal(tmp_path, monkeypatch):
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / ".focus"))
    config = tmp_path / "config.yaml"
    config.write_text(
        "models:\n"
        "  - name: demo\n"
        "    display_name: Demo\n"
        "    use: focus.models.deepseek:DeepSeekChatOpenAI\n"
        "    model: demo\n"
        "    api_key: $100\n"
        "    base_url: https://example.test\n",
        encoding="utf-8",
    )

    app_config = _load_layered_config(str(config))

    assert app_config.get_model("demo").api_key == "$100"


# === 密钥来源与优先级：进程环境变量优先于全局态 ~/.focus/.env ===


def _seed_global_env(tmp_path, monkeypatch, content: str):
    home = tmp_path / ".focus"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(content, encoding="utf-8")
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(home))


def test_global_dotenv_does_not_override_existing_environment(tmp_path, monkeypatch):
    _seed_global_env(tmp_path, monkeypatch, "FOCUS_TEST_PRECEDENCE=from-file\n")
    monkeypatch.setenv("FOCUS_TEST_PRECEDENCE", "from-env")

    load_global_dotenv()

    assert os.environ["FOCUS_TEST_PRECEDENCE"] == "from-env"


def test_global_dotenv_fills_missing_key(tmp_path, monkeypatch):
    _seed_global_env(tmp_path, monkeypatch, "FOCUS_TEST_FILLED=from-file\n")
    monkeypatch.delenv("FOCUS_TEST_FILLED", raising=False)

    load_global_dotenv()

    assert os.environ["FOCUS_TEST_FILLED"] == "from-file"


# === 使用期接线：MCP 服务器的环境变量引用 ===


def test_mcp_stdio_env_var_missing_names_server_and_variable():
    from focus.config.extensions_config import McpServerConfig
    from focus.mcp.client import build_server_params

    config = McpServerConfig(
        enabled=True,
        type="stdio",
        description="demo",
        command="npx",
        env={"TOKEN": f"${_MISSING}"},
    )

    with pytest.raises(ValueError) as excinfo:
        build_server_params("github", config)

    message = str(excinfo.value)
    assert "github" in message
    assert _MISSING in message


def test_mcp_stdio_env_var_resolved_when_present(monkeypatch):
    from focus.config.extensions_config import McpServerConfig
    from focus.mcp.client import build_server_params

    monkeypatch.setenv("FOCUS_TEST_PRESENT_KEY", "resolved-value")
    config = McpServerConfig(
        enabled=True,
        type="stdio",
        description="demo",
        command="npx",
        env={"TOKEN": "$FOCUS_TEST_PRESENT_KEY"},
    )

    assert build_server_params("github", config)["env"] == {"TOKEN": "resolved-value"}


# === 可选能力缺失不阻塞必需路径 ===


def test_optional_capability_missing_key_does_not_block_default_model(tmp_path, monkeypatch):
    """某项可选能力的密钥缺失时，配置加载成功且默认模型仍可装配。"""
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / ".focus"))
    monkeypatch.delenv("FOCUS_TEST_OPTIONAL_KEY", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        "models:\n"
        "  - name: main-model\n"
        "    display_name: main\n"
        "    use: focus.models.deepseek:DeepSeekChatOpenAI\n"
        "    model: main-model\n"
        "    api_key: test-key\n"
        "    base_url: https://example.test\n"
        "    default: true\n"
        "  - name: optional-capability-model\n"
        "    display_name: optional\n"
        "    use: focus.models.deepseek:DeepSeekChatOpenAI\n"
        "    model: optional-capability-model\n"
        "    api_key: $FOCUS_TEST_OPTIONAL_KEY\n"
        "    base_url: https://example.test\n",
        encoding="utf-8",
    )

    app_config = _load_layered_config(str(config))

    assert app_config.resolve_default_model_name() == "main-model"

    from focus.models import create_chat_model

    assert create_chat_model(app_config=app_config).model_name == "main-model"
