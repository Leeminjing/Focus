"""Regression coverage for manual document editing after removing spatial Agents.

Use the real plugin loader and signed save bridge, with in-memory session ports.
No Docker, model calls or production database is required.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from focus.plugins.interfaces import builtin_catalog
from focus.plugins.loader import load_plugins
from focus.plugins.registry import PluginRegistry
from plugins.spatial_patrol.docx_broker import BridgeContext, BrokerError, DocxCommandBroker
from plugins.spatial_patrol.docx_bridge import create_bridge_app
from plugins.spatial_patrol.docx_security import ScopedTokenSigner
from plugins.spatial_patrol.docx_sessions import SessionError, resolve_workspace_docx


def test_file_viewer_plugin_loads_without_a_model_and_only_exposes_editor_routes():
    registry = PluginRegistry(builtin_catalog())
    load_plugins(registry, Path(__file__).resolve().parents[2] / "plugins")
    record = next(item for item in registry.list_plugins() if item["name"] == "spatial-patrol")
    assert record["status"] == "active"
    assets = registry.active_assets()["spatial-patrol"]
    paths = {route.path for route in assets["router"].routes}
    assert "/docx/sessions" in paths
    assert "/docx/sessions/{session_id}/force-save" in paths
    assert "/docx/sessions/{session_id}/close" in paths
    assert all("anchor" not in path for path in paths)
    assert {tool.name for tool in registry.tools()} == {"demo_echo"}


def test_manual_editor_keeps_workspace_path_boundary(tmp_path):
    document = tmp_path / "example.docx"
    document.write_bytes(b"document bytes")
    assert resolve_workspace_docx(tmp_path, document.name) == document.resolve()
    outside = tmp_path.parent / "outside.docx"
    outside.write_bytes(b"outside")
    with pytest.raises(SessionError):
        resolve_workspace_docx(tmp_path, "../outside.docx")


def test_signed_save_bridge_roundtrip_and_dirty_event():
    async def run():
        changes = []
        edits = []

        class Sessions:
            async def update(self, session_id, **values):
                changes.append((session_id, values))

            async def record_edit_result(self, session_id, **values):
                edits.append((session_id, values))

        commands = DocxCommandBroker()
        commands.register(BridgeContext("session", "document", "", "", ""))
        signer = ScopedTokenSigner("save-bridge-test-secret-32-bytes-long")
        token = signer.issue(session_id="session", document_id="document",
                             scopes={"command:poll", "command:result", "event:write"})
        app = create_bridge_app(signer=signer, sessions=Sessions(), commands=commands)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as client:
            denied = await client.get("/bridge/commands/session/next", params={"access_token": "invalid"})
            assert denied.status_code == 404
            pending = asyncio.create_task(commands.issue("session", action="force_save", expected_version=3))
            response = await client.get("/bridge/commands/session/next", params={"access_token": token})
            assert response.status_code == 200
            command = response.json()["command"]
            assert command["action"] == "force_save"
            result = {"changed": False, "document_version": 3, "evidence": {"force_save_requested": True}}
            response = await client.post(f"/bridge/commands/session/{command['command_id']}/result",
                                         params={"access_token": token}, json=result)
            assert response.status_code == 200
            assert (await pending)["evidence"]["force_save_requested"]
            assert edits == [("session", {"changed": False, "error": None})]
            response = await client.post("/bridge/events/session", params={"access_token": token},
                                         json={"event": "documentChanged", "document_version": 4})
            assert response.status_code == 200
            assert changes == [("session", {"dirty": True, "status": "dirty"})]
            assert (await client.post("/bridge/projections/session", params={"access_token": token}, json={})).status_code == 404
        with pytest.raises(BrokerError, match="Unsupported"):
            await commands.issue("session", action="apply_edit")

    asyncio.run(run())


def test_editor_runtime_status_does_not_start_docker(monkeypatch):
    from plugins.spatial_patrol import docx_routes

    class Runtime:
        snapshot = SimpleNamespace(payload=lambda: {"state": "stopped"})

        async def prepare(self):
            pytest.fail("status inspection must not prepare Docker")

    monkeypatch.setattr(docx_routes, "runtime", Runtime())

    async def run():
        app = FastAPI()
        app.include_router(docx_routes.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://editor") as client:
            response = await client.get("/docx/runtime")
            assert response.status_code == 200
            assert response.json() == {"state": "stopped"}

    asyncio.run(run())
