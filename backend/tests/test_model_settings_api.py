"""模型设置的端到端验收：真实 Gateway + 真实文件层/用户偏好层。

输入为仓库自带的 config.yaml（文件层）、临时 `~/.focus`（用户偏好层，含注释与其它段落）；
输出为 HTTP 响应与磁盘文件。用例锁定：读模型不含密钥值、保存只改该改的键、保存即生效且无需
重启、被引用条目删除需要确认、以及用户偏好层在多次保存后仍保住注释与其它段落。

运行前置：可用的 PostgreSQL（conftest 会为本次会话建独立测试库并跑迁移）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "desktop-test")
pytestmark = pytest.mark.usefixtures("isolated_postgres_database")

from backend.app.gateway.app import app  # noqa: E402
from focus.config.app_config import (  # noqa: E402
    DEFAULT_MODEL_ENV_VAR,
    apply_app_config,
    build_app_config,
)
from focus.config.layered import load_layered_maps  # noqa: E402

SESSION = {"X-Focus-Session": "focus-dev-session"}

_ANNOTATED_PREFERENCE = """\
# 我自己写的说明，程序不该动它。
stream_bridge:
  type: memory
  queue_maxsize: 512
"""


@pytest.fixture(autouse=True)
def _clear_default_model_override(monkeypatch):
    """同 test_model_settings.py：宿主的 FOCUS_MODEL 会盖过配置声明的默认，且能让启动自检失败。"""
    monkeypatch.delenv(DEFAULT_MODEL_ENV_VAR, raising=False)


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    """临时用户偏好层 + 真实 Gateway；结束时把全局配置对象还原成仓库默认目录。"""
    home = tmp_path / ".focus"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FOCUS_GLOBAL_HOME", str(home))
    (home / "config.yaml").write_text(_ANNOTATED_PREFERENCE, encoding="utf-8")
    original_key = os.environ.get("OPENAI_API_KEY")

    file_map, _ = load_layered_maps("config.yaml", "config.yaml")
    file_bytes = Path("config.yaml").read_bytes()

    client = TestClient(app, client=("127.0.0.1", 50000))
    with client:
        yield client, client.app.state.desktop_service, home, file_map, file_bytes

    apply_app_config(client.app.state.desktop_service.app_config, build_app_config(file_map, {}))
    if original_key is None:
        os.environ.pop("OPENAI_API_KEY", None)
    else:
        os.environ["OPENAI_API_KEY"] = original_key


def test_settings_round_trip_applies_without_restart(desktop):
    client, service, home, file_map, file_bytes = desktop

    boot = client.get("/desktop/api/bootstrap", headers=SESSION).json()
    shipped_names = [entry["name"] for entry in boot["equipment"]["models"]]
    assert shipped_names, "仓库自带的 config.yaml 应当至少有一个模型条目"
    assert all(entry["source"] == "shipped" for entry in boot["equipment"]["models"])
    for entry in boot["equipment"]["models"]:
        assert "api_key_set" in entry
        assert entry["api_key_set"] is True

    before = client.get("/desktop/api/settings/models", headers=SESSION)
    snapshot = before.json()
    default_name = snapshot["default_model_name"]
    assert default_name in shipped_names
    assert set(snapshot["editable_fields"]) >= {"name", "base_url", "api_key", "context_window"}

    # 密钥值绝不出现在任何响应里
    secret = os.environ["OPENAI_API_KEY"]
    assert secret not in before.text
    assert secret not in json.dumps(boot)
    assert all(entry["api_key"].startswith("$") or entry["api_key"] == "" for entry in snapshot["models"])

    catalog = [
        {field: entry[field] for field in snapshot["editable_fields"]}
        for entry in snapshot["models"]
    ]
    for entry in catalog:
        if entry["name"] == default_name:
            entry["context_window"] = 65536
    probe = dict(catalog[0])
    probe.update({"name": "settings-probe", "display_name": "Settings Probe", "model": "settings-probe",
                  "default": False, "curation_default": False, "supports_image_input": False})
    catalog.append(probe)

    saved = client.put(
        "/desktop/api/settings/models",
        headers=SESSION,
        json={"models": catalog, "credentials": {"OPENAI_API_KEY": "rotated-test-key"}},
    )
    assert saved.status_code == 200, saved.text
    saved_snapshot = saved.json()
    names = {entry["name"]: entry for entry in saved_snapshot["models"]}
    assert names["settings-probe"]["source"] == "user_added"
    assert names[default_name]["source"] == "user_modified"
    assert names[default_name]["context_window"] == 65536
    assert "rotated-test-key" not in saved.text

    # 保存即生效：同一个进程、同一个客户端，不需要重启
    after_boot = client.get("/desktop/api/bootstrap", headers=SESSION).json()
    after_names = [entry["name"] for entry in after_boot["equipment"]["models"]]
    assert "settings-probe" in after_names
    assert service.app_config.resolve_default_model_name() == default_name
    assert service.app_config.get_model(default_name).context_window == 65536

    # 凭据写入用户偏好层，并在当前进程内立即可用
    assert "rotated-test-key" in (home / ".env").read_text(encoding="utf-8")
    assert os.environ["OPENAI_API_KEY"] == "rotated-test-key"

    # 文件层（这份程序自带的默认）一个字节都没被改
    assert Path("config.yaml").read_bytes() == file_bytes

    # 用户偏好层只增删改模型目录相关的键，注释与其它段落原样保留
    preference = (home / "config.yaml").read_text(encoding="utf-8")
    assert "# 我自己写的说明，程序不该动它。" in preference
    assert "queue_maxsize: 512" in preference
    assert "settings-probe" in preference
    assert "rotated-test-key" not in preference


def test_repeated_saves_keep_the_user_file_intact(desktop):
    client, _service, home, _file_map, _file_bytes = desktop
    snapshot = client.get("/desktop/api/settings/models", headers=SESSION).json()
    catalog = [
        {field: entry[field] for field in snapshot["editable_fields"]}
        for entry in snapshot["models"]
    ]

    for window in (32768, 16384, 8192):
        catalog[0]["context_window"] = window
        response = client.put("/desktop/api/settings/models", headers=SESSION, json={"models": catalog})
        assert response.status_code == 200, response.text

    text = (home / "config.yaml").read_text(encoding="utf-8")
    assert text.count("\nmodels:") == 1
    assert text.count("\nremoved_models:") == 1
    assert "# 我自己写的说明，程序不该动它。" in text
    assert "queue_maxsize: 512" in text
    assert "context_window: 8192" in text


def test_save_does_not_rewrite_already_persisted_run_entries(desktop):
    """保存只换配置目录，不追改已固化的运行记录：旧运行仍然指着原来那个条目名。"""
    client, service, _home, _file_map, _file_bytes = desktop
    from sqlalchemy import delete

    from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace

    snapshot = client.get("/desktop/api/settings/models", headers=SESSION).json()
    anchored = snapshot["models"][0]["name"]
    catalog = [
        {field: entry[field] for field in snapshot["editable_fields"]}
        for entry in snapshot["models"]
    ]
    for entry in catalog:
        if entry["name"] == anchored:
            entry["context_window"] = 32768

    task_id = "task-settings-anchor-01"
    workspace_id = "ws-settings-anchor-01"
    run_id = "run-settings-anchor-01"

    async def seed():
        async with service.session_factory() as session:
            session.add(DesktopWorkspace(workspace_id=workspace_id, path="C:/ws-anchor", display_name="anchor"))
            await session.flush()
            session.add(DesktopThread(task_id=task_id, workspace_id=workspace_id, thread_id="thread-anchor-01", title="anchor"))
            await session.flush()
            session.add(DesktopRun(run_id=run_id, task_id=task_id, agent_id="main", kind="main", status="success", model_name=anchored))
            await session.commit()

    async def read_run_model_name():
        async with service.session_factory() as session:
            row = await session.get(DesktopRun, run_id)
            return row.model_name if row else None

    async def cleanup():
        async with service.session_factory() as session:
            await session.execute(delete(DesktopRun).where(DesktopRun.run_id == run_id))
            await session.execute(delete(DesktopThread).where(DesktopThread.task_id == task_id))
            await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id))
            await session.commit()

    client.portal.call(seed)
    try:
        response = client.put("/desktop/api/settings/models", headers=SESSION, json={"models": catalog})
        assert response.status_code == 200, response.text

        assert client.portal.call(read_run_model_name) == anchored
        assert service.app_config.get_model(anchored).context_window == 32768
    finally:
        client.portal.call(cleanup)


def test_settings_endpoints_reject_non_loopback_callers(desktop):
    """设置写入只由人从本机界面发起：非 loopback 对等地址拿不到这些接口。"""
    client, _service, _home, _file_map, _file_bytes = desktop

    outsider = TestClient(app, client=("10.1.2.3", 50000))
    response = outsider.get("/desktop/api/settings/models", headers=SESSION)

    assert response.status_code == 404
    assert response.json()["detail"] == "Not Found"
    # 本机会话仍然可用，说明拒绝来自对等地址而不是路由不存在
    assert client.get("/desktop/api/settings/models", headers=SESSION).status_code == 200


def test_documented_database_override_wins_over_both_layers(desktop):
    """`FOCUS_DATABASE_URL` 是有文档化覆写键的配置项：它必须压过两层文件。"""
    _client, service, _home, _file_map, _file_bytes = desktop

    assert service.app_config.database is not None
    assert service.app_config.database.url == os.environ["FOCUS_DATABASE_URL"]
    assert "127.0.0.1:7221/focus" in Path("config.yaml").read_text(encoding="utf-8")


def test_referenced_entry_requires_confirmation_before_delete(desktop):
    client, service, _home, _file_map, _file_bytes = desktop
    from sqlalchemy import delete

    from backend.app.desktop.models import DesktopRun, DesktopThread, DesktopWorkspace

    snapshot = client.get("/desktop/api/settings/models", headers=SESSION).json()
    victim = snapshot["models"][0]["name"]
    catalog = [
        {field: entry[field] for field in snapshot["editable_fields"]}
        for entry in snapshot["models"]
        if entry["name"] != victim
    ]
    # 单例标记要交给留下的条目：删掉持有 default / curation_default 的条目时不能出现零声明
    if not any(entry["default"] for entry in catalog):
        catalog[0]["default"] = True
    if not any(entry["curation_default"] for entry in catalog):
        catalog[0]["curation_default"] = True
        if not catalog[0].get("curation_output_method"):
            catalog[0]["curation_output_method"] = "prompt_json"
    task_id = "task-settings-probe-01"
    workspace_id = "ws-settings-probe-01"
    run_id = "run-settings-probe-01"

    async def seed():
        async with service.session_factory() as session:
            session.add(DesktopWorkspace(workspace_id=workspace_id, path="C:/ws-settings", display_name="probe"))
            await session.flush()
            session.add(DesktopThread(task_id=task_id, workspace_id=workspace_id, thread_id="thread-settings-01", title="probe"))
            await session.flush()
            session.add(DesktopRun(run_id=run_id, task_id=task_id, agent_id="main", kind="main", status="success", model_name=victim))
            await session.commit()

    async def cleanup():
        async with service.session_factory() as session:
            await session.execute(delete(DesktopRun).where(DesktopRun.run_id == run_id))
            await session.execute(delete(DesktopThread).where(DesktopThread.task_id == task_id))
            await session.execute(delete(DesktopWorkspace).where(DesktopWorkspace.workspace_id == workspace_id))
            await session.commit()

    # 复用 TestClient 的 portal 事件循环：asyncpg 连接绑定在那个 loop 上，另起 loop 会报
    # "attached to a different loop"
    client.portal.call(seed)
    try:
        blocked = client.put("/desktop/api/settings/models", headers=SESSION, json={"models": catalog})
        assert blocked.status_code == 409, blocked.text
        detail = blocked.json()["detail"]
        assert detail["code"] == "model_referenced"
        assert victim in detail["references"]
        assert any(item["kind"] == "run" for item in detail["references"][victim])

        # 未确认：目录没变
        still = client.get("/desktop/api/settings/models", headers=SESSION).json()
        assert victim in [entry["name"] for entry in still["models"]]

        confirmed = client.put(
            "/desktop/api/settings/models",
            headers=SESSION,
            json={"models": catalog, "acknowledge_references": True},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert victim not in [entry["name"] for entry in confirmed.json()["models"]]
        assert victim in confirmed.json()["removed_models"]
    finally:
        client.portal.call(cleanup)
