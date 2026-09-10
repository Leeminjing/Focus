"""全新安装的最小配置契约测试。

复刻安装器行为——由 `.env.example` 种下 `~/.focus/.env`、写入空的全局兜底 config.yaml——
并断言：仅凭文档所述必需凭据，仓库态配置即可加载且默认模型可装配。

这是「新装即可启动」要求的回归测试：变更前仓库 config.yaml 引用 `$VISION_API_KEY`，
缺失时配置加载期直接抛 KeyError，新装用户无法启动。
"""

import shutil
from pathlib import Path

import pytest

from focus.config.app_config import _load_layered_config
from focus.config.layered import load_global_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REQUIRED_CREDENTIALS = {"OPENAI_API_KEY"}
_EXPECTED_DEFAULT_MODEL = "deepseek-v4-flash-vision-exp"


@pytest.fixture
def fresh_install(tmp_path, monkeypatch):
    """模拟安装后的全局态，并清空所有可能从外部泄漏进来的密钥变量。"""
    home = tmp_path / ".focus"
    home.mkdir(parents=True)
    shutil.copy(_REPO_ROOT / ".env.example", home / ".env")
    (home / "config.yaml").write_text(
        "# Focus global configuration fallback layer.\n", encoding="utf-8"
    )
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(home))
    for key in ("OPENAI_API_KEY", "VISION_API_KEY", "FOCUS_MODEL", "FOCUS_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    return home


def _seeded_keys(env_file: Path) -> set[str]:
    return {
        line.split("=", 1)[0].strip()
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def test_seeded_credential_template_lists_exactly_the_required_keys(fresh_install):
    """安装器种下的模板既不能漏必需键，也不能含已废弃键（如 VISION_API_KEY）。"""
    assert _seeded_keys(fresh_install / ".env") == _REQUIRED_CREDENTIALS


def test_shipped_config_loads_with_only_the_documented_credential(fresh_install):
    load_global_dotenv()

    app_config = _load_layered_config(str(_REPO_ROOT / "config.yaml"))

    assert app_config.resolve_default_model_name() == _EXPECTED_DEFAULT_MODEL
    assert [
        model.name for model in app_config.models if model.curation_default
    ] == [_EXPECTED_DEFAULT_MODEL]


def test_default_model_assembles_with_only_the_documented_credential(fresh_install):
    load_global_dotenv()
    app_config = _load_layered_config(str(_REPO_ROOT / "config.yaml"))

    from focus.models import create_chat_model

    model = create_chat_model(app_config=app_config)

    assert model.model_name == _EXPECTED_DEFAULT_MODEL


def test_every_shipped_model_entry_assembles_with_the_shared_credential(fresh_install):
    """仓库目录中的每个条目都只依赖同一个必需凭据，因此都应可装配。"""
    load_global_dotenv()
    app_config = _load_layered_config(str(_REPO_ROOT / "config.yaml"))

    from focus.models import create_chat_model

    for entry in app_config.models:
        assert create_chat_model(name=entry.name, app_config=app_config) is not None
