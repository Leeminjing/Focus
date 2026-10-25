"""本文件对外提供联网工具 YAML 配置的加载、覆盖与使用期凭据校验回归。

输入为文件层、全局偏好和环境变量；输出为实际工具收到的鉴权头或可恢复错误。
具体工作流为复用 AppConfig 单例、分层加载与就地生效，不读取用户真实凭据。
示例：pytest backend/tests/test_web_config.py。
"""

import asyncio

import httpx
import pytest
from focus.config import app_config as config_module
from focus.config.app_config import (
    AppConfig,
    apply_app_config,
    build_app_config,
    get_app_config,
)
from focus.config.layered import load_global_dotenv
from focus.config.web_config import get_deepseek_api_key
from focus.tools.builtins import web_tools
from langchain_core.tools import ToolException


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(tmp_path / "global"))
    monkeypatch.setattr(config_module, "_app_config", None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("WEB_TEST_API_KEY", raising=False)


def _load_yaml(tmp_path, api_key):
    path = tmp_path / "config.yaml"
    path.write_text(f"models: []\nweb_tools:\n  api_key: {api_key}\n", encoding="utf-8")
    return get_app_config(str(path))


def test_missing_web_section_uses_independent_default_without_requiring_key(monkeypatch):
    config = build_app_config({"models": []}, {})
    monkeypatch.setattr(config_module, "_app_config", config)
    assert config.web_tools.api_key == "$DEEPSEEK_API_KEY"
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        get_deepseek_api_key()


@pytest.mark.parametrize("reference", ["$WEB_TEST_API_KEY", "${WEB_TEST_API_KEY}"])
def test_yaml_reference_uses_global_dotenv_and_process_precedence(tmp_path, monkeypatch, reference):
    home = tmp_path / "global"
    home.mkdir()
    (home / ".env").write_text("WEB_TEST_API_KEY=sk-file\n", encoding="utf-8")
    monkeypatch.setenv("WEB_TEST_API_KEY", "sk-process")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-other")
    load_global_dotenv()
    config = _load_yaml(tmp_path, reference)
    assert config.web_tools.api_key == "sk-process"
    assert get_deepseek_api_key() == "sk-process"


def test_yaml_reference_loads_key_from_global_dotenv(tmp_path):
    home = tmp_path / "global"
    home.mkdir()
    (home / ".env").write_text("WEB_TEST_API_KEY=sk-file\n", encoding="utf-8")
    load_global_dotenv()
    _load_yaml(tmp_path, "$WEB_TEST_API_KEY")
    assert get_deepseek_api_key() == "sk-file"


def test_yaml_literal_key_is_used_instead_of_default_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-other")
    _load_yaml(tmp_path, "sk-explicit-yaml")
    assert get_deepseek_api_key() == "sk-explicit-yaml"


def test_global_yaml_override_wins_over_shipped_web_key(tmp_path, monkeypatch):
    home = tmp_path / "global"
    home.mkdir()
    (home / "config.yaml").write_text("web_tools:\n  api_key: $WEB_TEST_API_KEY\n", encoding="utf-8")
    monkeypatch.setenv("WEB_TEST_API_KEY", "sk-user")
    _load_yaml(tmp_path, "sk-shipped")
    assert get_deepseek_api_key() == "sk-user"


@pytest.mark.parametrize("tool, args", [(web_tools.web_search, {"query": "python"}),
    (web_tools.web_fetch, {"url": "https://example.com"})])
def test_unresolved_config_reference_only_fails_when_tool_runs(tmp_path, monkeypatch, tool, args):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-not-fallback")
    config = _load_yaml(tmp_path, "$WEB_TEST_API_KEY")
    assert config.web_tools.api_key == "$WEB_TEST_API_KEY"

    def no_network(**kwargs):
        pytest.fail("missing configured key must fail before HTTP")
    monkeypatch.setattr(web_tools, "AsyncClient", no_network)
    with pytest.raises(ToolException, match="WEB_TEST_API_KEY"):
        asyncio.run(tool.ainvoke(args))


def test_unresolved_reference_can_be_filled_after_config_load(tmp_path, monkeypatch):
    _load_yaml(tmp_path, "$WEB_TEST_API_KEY")
    monkeypatch.setenv("WEB_TEST_API_KEY", "sk-added-later")
    assert get_deepseek_api_key() == "sk-added-later"


@pytest.mark.parametrize("key", ["''", "'   '"])
def test_empty_yaml_key_does_not_fallback_to_environment(tmp_path, monkeypatch, key):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-not-fallback")
    _load_yaml(tmp_path, key)
    with pytest.raises(ValueError, match="web_tools.api_key"):
        get_deepseek_api_key()


def test_apply_updates_key_used_by_existing_tool_objects(tmp_path, monkeypatch):
    target = _load_yaml(tmp_path, "sk-before")
    held_tool = web_tools.web_fetch
    seen = []

    def handle(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"title": "Page", "content": "# Page"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(web_tools, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handle), trust_env=False,
    ))

    async def run():
        await held_tool.ainvoke({"url": "https://example.com"})
        fresh = build_app_config({"models": [], "web_tools": {"api_key": "sk-after"}}, {})
        assert apply_app_config(target, fresh) is target
        await held_tool.ainvoke({"url": "https://example.com"})

    asyncio.run(run())
    assert seen == ["Bearer sk-before", "Bearer sk-after"]


def test_default_config_objects_do_not_share_mutable_web_settings():
    first = AppConfig(models=[])
    second = AppConfig(models=[])
    first.web_tools.api_key = "sk-first"
    assert second.web_tools.api_key == "$DEEPSEEK_API_KEY"
