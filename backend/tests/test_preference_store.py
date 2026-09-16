"""用户偏好层读写（保注释往返、原子写入、凭据边界）的单测。

输入为临时 `.focus` 家目录下的 config.yaml / .env；输出为写入结果与文件内容。
用例锁定：只改模型目录相关键、注释与其它段落保留、失败拒绝写入、凭据不回显。
"""

import os
from pathlib import Path

import pytest

from focus.config.preference_store import (
    PreferenceWriteError,
    apply_credential,
    credential_is_set,
    preference_config_path,
    preference_env_path,
    read_preference_config,
    write_models_preference,
)

_GLOBAL_ENV = "FOCUS_GLOBAL_HOME"

_ANNOTATED_CONFIG = """\
# 用户偏好层：这里是我自己写下的说明，程序不该动它。
stream_bridge:
  type: memory
  queue_maxsize: 512

# 模型目录由桌面设置维护；手工编辑同样受支持。
models:
  - name: mine
    display_name: Mine
    use: focus.models.deepseek:DeepSeekChatOpenAI
    model: mine
    api_key: $OPENAI_API_KEY
    base_url: https://api.example.com
    context_window: 8192
"""


@pytest.fixture
def focus_home(tmp_path, monkeypatch):
    home = tmp_path / ".focus"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(_GLOBAL_ENV, str(home))
    assert os.environ[_GLOBAL_ENV] == str(home)
    return home


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


def test_other_sections_and_comments_survive(focus_home):
    (focus_home / "config.yaml").write_text(_ANNOTATED_CONFIG, encoding="utf-8")

    write_models_preference([_entry("replacement")], [], "config.yaml")

    text = (focus_home / "config.yaml").read_text(encoding="utf-8")
    assert "# 用户偏好层：这里是我自己写下的说明，程序不该动它。" in text
    assert "# 模型目录由桌面设置维护；手工编辑同样受支持。" in text
    assert "queue_maxsize: 512" in text
    document = read_preference_config("config.yaml")
    assert list(document.keys()) == ["stream_bridge", "models", "removed_models"]
    assert [entry["name"] for entry in document["models"]] == ["replacement"]


def test_key_order_is_stable_across_repeated_saves(focus_home):
    (focus_home / "config.yaml").write_text(_ANNOTATED_CONFIG, encoding="utf-8")

    write_models_preference([_entry("a")], [], "config.yaml")
    first = read_preference_config("config.yaml")
    write_models_preference([_entry("b")], ["gone"], "config.yaml")
    second = read_preference_config("config.yaml")

    assert list(first.keys()) == list(second.keys())
    assert list(second.keys()) == ["stream_bridge", "models", "removed_models"]
    assert list(second["removed_models"]) == ["gone"]


def test_missing_file_is_created(focus_home):
    path = preference_config_path("config.yaml")
    assert not path.is_file()

    write_models_preference([_entry("fresh")], ["legacy"], "config.yaml")

    assert path.is_file()
    document = read_preference_config("config.yaml")
    assert [entry["name"] for entry in document["models"]] == ["fresh"]
    assert list(document["removed_models"]) == ["legacy"]


def test_comment_only_file_is_not_treated_as_broken(focus_home):
    """安装器种下的占位文件只有注释、没有任何节点。

    ruamel 在流模式下把这种文件判为 0 个文档；若把它当异常，全新安装的用户会在第一次保存时被
    永久卡住（真实故障）。它必须按空配置处理，同时把那句注释原样留在文件顶部。
    """
    path = focus_home / "config.yaml"
    path.write_text("# Focus 全局配置默认层（仓库/项目态优先）\n", encoding="utf-8")

    write_models_preference([_entry("fresh")], [], "config.yaml")

    text = path.read_text(encoding="utf-8")
    assert "# Focus 全局配置默认层（仓库/项目态优先）" in text
    assert "fresh" in text
    document = read_preference_config("config.yaml")
    assert [entry["name"] for entry in document["models"]] == ["fresh"]


def test_whitespace_only_file_is_treated_as_empty(focus_home):
    path = focus_home / "config.yaml"
    path.write_text("   \n\n\t\n", encoding="utf-8")

    write_models_preference([_entry("fresh")], [], "config.yaml")

    assert read_preference_config("config.yaml")["models"][0]["name"] == "fresh"


def test_unparsable_file_is_refused_and_left_alone(focus_home):
    path = focus_home / "config.yaml"
    broken = "models: [\n  - name: 'unterminated\n"
    path.write_text(broken, encoding="utf-8")
    before = path.stat().st_mtime_ns

    with pytest.raises(PreferenceWriteError):
        write_models_preference([_entry("a")], [], "config.yaml")

    assert path.read_text(encoding="utf-8") == broken
    assert path.stat().st_mtime_ns == before


def test_multi_document_file_is_refused(focus_home):
    path = focus_home / "config.yaml"
    path.write_text("models: []\n---\nmodels: []\n", encoding="utf-8")

    with pytest.raises(PreferenceWriteError):
        read_preference_config("config.yaml")


def test_non_mapping_top_level_is_refused(focus_home):
    (focus_home / "config.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")

    with pytest.raises(PreferenceWriteError):
        write_models_preference([_entry("a")], [], "config.yaml")


def test_atomic_write_leaves_no_temporary_file(focus_home):
    write_models_preference([_entry("a")], [], "config.yaml")

    leftovers = [p.name for p in focus_home.iterdir() if p.name.startswith(".config.yaml.tmp")]
    assert leftovers == []


def test_interrupted_write_keeps_the_original_file(focus_home, monkeypatch):
    """替换那一步失败（模拟中断）：原文件保持完整，且不留下临时文件。"""
    path = focus_home / "config.yaml"
    path.write_text(_ANNOTATED_CONFIG, encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    def interrupted(_source, _target):
        raise OSError("simulated interruption")

    monkeypatch.setattr("os.replace", interrupted)

    with pytest.raises(PreferenceWriteError):
        write_models_preference([_entry("replacement")], [], "config.yaml")

    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in focus_home.iterdir() if p.name.startswith(".config.yaml.tmp")] == []


def test_external_edit_during_runtime_is_kept(focus_home):
    (focus_home / "config.yaml").write_text(_ANNOTATED_CONFIG, encoding="utf-8")
    read_preference_config("config.yaml")  # 模拟运行期已加载过

    # 运行期间用户在外部手工加了新段落
    path = focus_home / "config.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "\ncommitment:\n  enabled: true\n",
        encoding="utf-8",
    )

    write_models_preference([_entry("a")], [], "config.yaml")

    document = read_preference_config("config.yaml")
    assert document["commitment"]["enabled"] is True


def test_credential_write_preserves_other_keys(focus_home):
    env_path = focus_home / ".env"
    env_path.write_text("# 我的密钥\nOPENAI_API_KEY=old\nOTHER_KEY=keep\n", encoding="utf-8")

    apply_credential("OPENAI_API_KEY", "new-value")

    text = env_path.read_text(encoding="utf-8")
    assert "# 我的密钥" in text
    assert "OTHER_KEY=keep" in text
    assert "new-value" in text
    assert "old" not in text


def test_credential_write_appends_missing_key(focus_home):
    env_path = focus_home / ".env"
    env_path.write_text("OTHER_KEY=keep\n", encoding="utf-8")

    apply_credential("OPENAI_API_KEY", "fresh")

    text = env_path.read_text(encoding="utf-8")
    assert "OTHER_KEY=keep" in text
    assert "OPENAI_API_KEY=fresh" in text


def test_credential_value_with_spaces_is_quoted(focus_home):
    apply_credential("SOME_KEY", "has space")

    text = preference_env_path().read_text(encoding="utf-8")
    assert 'SOME_KEY="has space"' in text


def test_credential_applies_inside_current_process(focus_home, monkeypatch):
    monkeypatch.delenv("FOCUS_TEST_KEY", raising=False)
    assert credential_is_set("FOCUS_TEST_KEY") is False

    apply_credential("FOCUS_TEST_KEY", "live-value")

    assert os.environ["FOCUS_TEST_KEY"] == "live-value"
    assert credential_is_set("FOCUS_TEST_KEY") is True


def test_empty_credential_value_keeps_existing_key(focus_home):
    env_path = focus_home / ".env"
    env_path.write_text("OPENAI_API_KEY=keep-me\n", encoding="utf-8")

    apply_credential("OPENAI_API_KEY", "")

    assert "keep-me" in env_path.read_text(encoding="utf-8")


def test_credential_rejects_newlines(focus_home):
    with pytest.raises(PreferenceWriteError):
        apply_credential("SOME_KEY", "line1\nline2")


def test_apply_credential_returns_nothing_carries_no_value(focus_home):
    result = apply_credential("SOME_KEY", "secret-value")

    assert result is None


def test_paths_follow_the_global_home(focus_home):
    assert preference_config_path("config.yaml") == focus_home / "config.yaml"
    assert preference_env_path() == focus_home / ".env"
    assert isinstance(focus_home, Path)
