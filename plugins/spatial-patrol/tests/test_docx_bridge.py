from fastapi.testclient import TestClient

import asyncio
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin

import jwt
import pytest

from plugins.spatial_patrol import docx_bridge
from plugins.spatial_patrol.docx_bridge import (
    BridgeServer,
    BridgeUnavailable,
    _document_server_token,
    callback_transition,
    create_bridge_app,
)
from plugins.spatial_patrol.docx_broker import BridgeContext, DocxCommandBroker
from plugins.spatial_patrol.docx_security import ScopedTokenSigner


class FailingSessions:
    async def get(self, _session_id):
        raise RuntimeError("database down")


class CapturingSessions:
    def __init__(self):
        self.record = SimpleNamespace(document_id="document-a", status="ready")
        self.updates = []
        self.edit_results = []

    async def get(self, _session_id):
        return self.record

    async def update(self, session_id, **changes):
        self.updates.append((session_id, changes))
        return self.record

    async def record_edit_result(self, session_id, **result):
        self.edit_results.append((session_id, result))


class CapturingCommands:
    def __init__(self):
        self.bridge_context = BridgeContext(
            session_id="session-a",
            document_id="document-a",
            plugin_url="http://127.0.0.1/bridge/index.html",
            browser_bridge_origin="http://127.0.0.1:18081",
            document_server_origin="http://127.0.0.1:18080",
        )
        self.completed = []

    def context(self, _session_id):
        return self.bridge_context

    def complete(self, command_id, payload):
        self.completed.append((command_id, payload))


def test_bridge_health_is_independent_of_document_services():
    app = create_bridge_app(
        signer=ScopedTokenSigner("s" * 48),
        sessions=FailingSessions(),
        commands=DocxCommandBroker(),
    )
    client = TestClient(app)
    assert client.get("/health").json() == {"ok": True}
    response = client.get("/bridge/documents/missing/document.docx?access_token=bad")
    assert response.status_code >= 400
    assert "database down" not in response.text


def test_plugin_config_rejects_cross_session_token_without_leaking_context():
    commands = DocxCommandBroker()
    app = create_bridge_app(
        signer=ScopedTokenSigner("s" * 48), sessions=FailingSessions(), commands=commands,
    )
    response = TestClient(app).get("/bridge/plugin-config/a?access_token=bad")
    assert response.status_code == 404


def test_plugin_config_directory_serves_the_onlyoffice_bridge_assets():
    signer = ScopedTokenSigner("s" * 48)
    commands = DocxCommandBroker()
    load_token = signer.issue(
        session_id="session-a",
        document_id="document-a",
        scopes={"plugin:load"},
    )
    command_token = signer.issue(
        session_id="session-a",
        document_id="document-a",
        scopes={"command:poll", "command:result", "event:write", "projection:write"},
    )
    commands.register(BridgeContext(
        session_id="session-a",
        document_id="document-a",
        plugin_url=(
            "http://127.0.0.1:5000/plugins/spatial-patrol/desktop/"
            "onlyoffice-focus-bridge/index.html?"
            f"bridge_origin=http%3A%2F%2F127.0.0.1%3A18081&"
            f"session_id=session-a&access_token={command_token}"
        ),
        browser_bridge_origin="http://127.0.0.1:18081",
        document_server_origin="http://127.0.0.1:18080",
    ))
    app = create_bridge_app(
        signer=signer, sessions=FailingSessions(), commands=commands,
    )
    client = TestClient(app)

    config_url = f"/bridge/plugin-config/session-a/{load_token}/config.json"
    config = client.get(config_url)

    assert config.status_code == 200
    variation_url = config.json()["variations"][0]["url"]
    entry_url = urljoin(config_url, variation_url) + "?lang=zh-CN&theme-type=light"
    entry = client.get(entry_url)
    code = client.get(urljoin(entry_url, "code.js"))
    sdk_config = client.get(urljoin(entry_url, "./config.json"))

    assert variation_url == f"{command_token}/index.html"
    assert "access_token" not in variation_url
    assert entry.status_code == 200
    assert entry.headers["content-type"].startswith("text/html")
    assert "code.js" in entry.text
    assert code.status_code == 200
    assert "Asc.plugin" in code.text
    assert sdk_config.status_code == 200
    assert sdk_config.json()["guid"] == config.json()["guid"]
    assert client.get(
        "/bridge/plugin-config/session-a/bad/config.json"
    ).status_code == 404
    assert client.get(
        f"/bridge/plugin-config/session-a/{load_token}/secret.txt"
    ).status_code == 404
    assert client.get(
        f"/bridge/plugin-config/session-a/{command_token}/config.json"
    ).status_code == 404
    assert client.get(
        f"/bridge/plugin-config/session-a/{load_token}/bad/index.html"
    ).status_code == 404
    assert client.get(
        f"/bridge/plugin-config/session-a/bad/{command_token}/index.html"
    ).status_code == 404
    assert client.get(
        "/bridge/commands/session-a/next",
        params={"timeout": 0, "access_token": load_token},
    ).status_code == 404
    assert client.get(
        "/bridge/commands/session-a/next",
        params={"timeout": 0, "access_token": command_token},
    ).status_code == 200


def test_callback_status_keeps_editor_dirty_and_disk_saved_states_distinct():
    assert callback_transition(1, dirty=False) == ("editing", False)
    assert callback_transition(2, dirty=True) == ("saving", True)
    assert callback_transition(6, dirty=True) == ("saving", True)
    assert callback_transition(4, dirty=True) == ("recoverable", False)
    assert callback_transition(4, dirty=False) == ("closed", False)
    assert callback_transition(7, dirty=True) == ("recoverable", False)


def test_command_result_accepts_observation_projection_and_rejects_unknown_fields():
    signer = ScopedTokenSigner("s" * 48)
    sessions = CapturingSessions()
    commands = CapturingCommands()
    app = create_bridge_app(signer=signer, sessions=sessions, commands=commands)
    client = TestClient(app)
    access_token = signer.issue(
        session_id="session-a",
        document_id="document-a",
        scopes={"command:result"},
    )
    payload = {
        "changed": False,
        "document_version": 1,
        "target_id": "paragraph:abc",
        "observation": {
            "target_id": "paragraph:abc",
            "kind": "paragraph",
            "content": "摘要",
            "format": {"style": "Normal"},
        },
        "projection": {
            "target_id": "paragraph:abc",
            "page": 1,
            "rect": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1},
        },
    }

    response = client.post(
        f"/bridge/commands/session-a/command-a/result?access_token={access_token}",
        json=payload,
    )

    assert response.json() == {"ok": True}
    assert commands.completed == [("command-a", {**payload, "evidence": {}})]
    assert client.post(
        f"/bridge/commands/session-a/command-b/result?access_token={access_token}",
        json={**payload, "unknown": True},
    ).status_code == 422


def test_callback_prefers_onlyoffice_body_token_over_bearer_header():
    assert _document_server_token(
        {"token": "body.jwt.token"}, "Bearer header.jwt.token"
    ) == "body.jwt.token"


def test_callback_accepts_onlyoffice_bearer_header_when_body_token_is_absent():
    assert _document_server_token({}, "Bearer header.jwt.token") == "header.jwt.token"


def test_successful_editing_callback_clears_a_stale_session_error():
    signer = ScopedTokenSigner("s" * 48)
    sessions = CapturingSessions()
    app = create_bridge_app(
        signer=signer,
        sessions=sessions,
        commands=DocxCommandBroker(),
        ds_runtime=SimpleNamespace(jwt_secret="d" * 48),
    )
    access_token = signer.issue(
        session_id="session-a",
        document_id="document-a",
        scopes={"callback:write"},
    )
    document_server_token = jwt.encode(
        {"status": 1}, "d" * 48, algorithm="HS256"
    )

    response = TestClient(app).post(
        f"/bridge/callback/session-a?access_token={access_token}",
        json={"status": 1, "token": document_server_token},
    )

    assert response.json() == {"error": 0}
    assert sessions.updates[-1] == (
        "session-a",
        {"status": "editing", "callback_status": 1, "last_error": None},
    )


def test_late_callback_cannot_revive_a_terminal_session():
    signer = ScopedTokenSigner("s" * 48)
    sessions = CapturingSessions()
    sessions.record.status = "closed"
    sessions.record.dirty = True
    sessions.record.saved_hash = "saved-before-close"
    sessions.record.document_version = 7
    app = create_bridge_app(
        signer=signer,
        sessions=sessions,
        commands=DocxCommandBroker(),
        ds_runtime=SimpleNamespace(jwt_secret="d" * 48),
    )
    access_token = signer.issue(
        session_id="session-a",
        document_id="document-a",
        scopes={"callback:write"},
    )

    response = TestClient(app).post(
        f"/bridge/callback/session-a?access_token={access_token}",
        json={"status": 1},
    )

    assert response.json() == {"error": 0}
    assert sessions.updates == []
    assert sessions.record.status == "closed"
    assert sessions.record.dirty is True
    assert sessions.record.saved_hash == "saved-before-close"
    assert sessions.record.document_version == 7


def test_bridge_startup_failure_is_reported_only_as_bridge_unavailable(monkeypatch):
    class DeadServer:
        should_exit = False

        def __init__(self, _config):
            pass

        async def serve(self):
            raise RuntimeError("bridge crashed")

    monkeypatch.setattr(docx_bridge, "runtime", SimpleNamespace(bridge_secret="s" * 48))
    monkeypatch.setattr(docx_bridge, "_EmbeddedServer", DeadServer)
    server = BridgeServer(port=18991)
    with pytest.raises(BridgeUnavailable, match="failed to start"):
        asyncio.run(server.prepare())

    # The independent bridge app itself remains healthy even after service startup fails.
    app = create_bridge_app(
        signer=ScopedTokenSigner("s" * 48), sessions=FailingSessions(),
        commands=DocxCommandBroker(),
    )
    assert TestClient(app).get("/health").json() == {"ok": True}


def test_bridge_serves_on_the_calling_event_loop(monkeypatch):
    observed = {}

    class SameLoopServer:
        should_exit = False

        def __init__(self, _config):
            pass

        async def serve(self):
            observed["loop"] = asyncio.get_running_loop()
            while not self.should_exit:
                await asyncio.sleep(0)

    async def scenario():
        monkeypatch.setattr(docx_bridge, "runtime", SimpleNamespace(bridge_secret="s" * 48))
        monkeypatch.setattr(docx_bridge, "_EmbeddedServer", SameLoopServer)
        server = BridgeServer(port=18992)
        server._healthy = lambda: asyncio.sleep(0, result="loop" in observed)
        await server.prepare()
        assert observed["loop"] is asyncio.get_running_loop()
        assert server._task is not None
        server.stop()
        await server._task

    asyncio.run(scenario())


def test_docx_editor_spans_all_rows_of_the_host_file_panel_grid():
    css = (Path(__file__).parents[1] / "desktop" / "docx-editor.css").read_text(
        encoding="utf-8"
    )
    assert ".file-panel-inner > .focus-docx-editor" in css
    assert "grid-row: 1 / -1" in css
