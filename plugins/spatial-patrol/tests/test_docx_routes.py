"""DOCX 控制面路由测试。

本文件验证浏览器编辑器收到可直接访问的插件配置 URL。输入为隔离的运行时、
会话管理器与 Bridge 替身；输出为完整 DocsAPI 配置；工作流只执行会话创建，
不启动 Docker、数据库或 Focus 主服务。
"""

import asyncio
from types import SimpleNamespace
from urllib.parse import urlparse

from plugins.spatial_patrol import docx_routes


class _Record:
    session_id = "session-a"
    document_id = "document-a"
    document_version = 1
    saved_hash = "a" * 64
    permissions = {"edit": True}
    status = "ready"

    def to_payload(self):
        return {"session_id": self.session_id}


def test_session_gives_browser_a_browser_reachable_plugin_config(monkeypatch):
    record = _Record()
    fake_runtime = SimpleNamespace(
        bridge_secret="b" * 48,
        jwt_secret="j" * 48,
        snapshot=SimpleNamespace(document_server_url="http://127.0.0.1:18080"),
        prepare=lambda: asyncio.sleep(0),
        session_opened=lambda _session_id: None,
    )
    fake_bridge = SimpleNamespace(
        public_origin="http://127.0.0.1:18081",
        container_origin="http://host.docker.internal:18081",
        prepare=lambda: asyncio.sleep(0),
    )
    fake_sessions = SimpleNamespace(
        acquire=lambda **_kwargs: asyncio.sleep(0, result=(record, False)),
    )
    registered = []
    fake_broker = SimpleNamespace(register=registered.append)
    monkeypatch.setattr(docx_routes, "runtime", fake_runtime)
    monkeypatch.setattr(docx_routes, "bridge_server", fake_bridge)
    monkeypatch.setattr(docx_routes, "session_manager", fake_sessions)
    monkeypatch.setattr(docx_routes, "broker", fake_broker)

    payload = asyncio.run(docx_routes.create_session(
        docx_routes.CreateSession(task_id="task-a", content_ref="rich.docx", mode="edit"),
        SimpleNamespace(base_url="http://127.0.0.1:52402/"),
    ))

    plugin_url = payload["config"]["editorConfig"]["plugins"]["pluginsData"][0]
    assert plugin_url.startswith("http://127.0.0.1:18081/bridge/plugin-config/")
    parsed_plugin_url = urlparse(plugin_url)
    assert parsed_plugin_url.query == ""
    assert parsed_plugin_url.path.endswith("/config.json")
    assert len(parsed_plugin_url.path.split("/")[-2]) > 32
    assert registered[0].browser_bridge_origin == "http://127.0.0.1:18081"


def test_new_anchor_resolves_the_exact_clicked_editor_point():
    calls = []

    class _Broker:
        async def issue(self, session_id, **command):
            calls.append((session_id, command))
            return {
                "changed": False,
                "document_version": 3,
                "observation": {
                    "target_id": "paragraph:current",
                    "kind": "paragraph",
                    "content": "current paragraph",
                },
                "projection": {
                    "target_id": "paragraph:current",
                    "page": 2,
                    "point": {"x": 0.37, "y": 0.64},
                    "rect": {"x": 0.37, "y": 0.64, "width": 0, "height": 0},
                    "viewport_rect": {"x": 412, "y": 533, "width": 0, "height": 0},
                },
            }

    observed = asyncio.run(docx_routes._resolve_anchor_point(
        _Broker(), "session-a", 3, viewport_x=412, viewport_y=533,
    ))

    assert observed["target_id"] == "paragraph:current"
    assert observed["projection"]["page"] == 2
    assert calls == [("session-a", {
        "action": "resolve_anchor_point",
        "arguments": {"viewport_x": 412, "viewport_y": 533},
        "expected_version": 3,
        "timeout": 10,
    })]


def test_anchor_request_rejects_the_old_empty_body():
    try:
        docx_routes.CreateAnchor.model_validate({})
    except ValueError as exc:
        assert "viewport_x" in str(exc)
        assert "viewport_y" in str(exc)
    else:
        raise AssertionError("empty anchor requests must not reuse the old cursor")
