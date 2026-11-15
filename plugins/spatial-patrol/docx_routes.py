"""Focus-authenticated manual DOCX editing sessions, save and close routes."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from plugins.spatial_patrol.docx_broker import BridgeContext, BrokerError, broker
from plugins.spatial_patrol.docx_bridge import BridgeUnavailable, bridge_server
from plugins.spatial_patrol.docx_runtime import RuntimeUnavailable, runtime
from plugins.spatial_patrol.docx_security import ScopedTokenSigner
from plugins.spatial_patrol.docx_sessions import (
    SessionConflict,
    SessionError,
    session_manager,
)


router = APIRouter(prefix="/docx", tags=["spatial-patrol-docx"])
BRIDGE_GUID = "asc.{8F6F5C83-24D7-4DB3-9338-07384F50C764}"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSession(_Strict):
    task_id: str = Field(min_length=1)
    content_ref: str = Field(min_length=1)
    mode: str = Field(pattern=r"^(view|edit)$")


class CloseSession(_Strict):
    discard_unsaved: bool = False


def _bridge_url(origin: str, path: str, token: str) -> str:
    return f"{origin}{path}?{urlencode({'access_token': token})}"


@router.get("/runtime")
async def runtime_status() -> dict[str, Any]:
    """Inspect state without starting Docker."""
    return runtime.snapshot.payload()


@router.post("/sessions")
async def create_session(body: CreateSession, request: Request) -> dict[str, Any]:
    """Prepare the optional runtime when the user first opens a DOCX document."""
    try:
        await runtime.prepare()
        await bridge_server.prepare()
        record, resumed = await session_manager.acquire(
            task_id=body.task_id,
            content_ref=body.content_ref,
            mode=body.mode,
        )
    except SessionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except SessionError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (RuntimeUnavailable, BridgeUnavailable) as exc:
        raise HTTPException(503, f"DOCX 编辑插件不可用: {exc}") from exc

    signer = ScopedTokenSigner(runtime.bridge_secret)
    common = {
        "session_id": record.session_id,
        "document_id": record.document_id,
        "ttl_seconds": 4 * 60 * 60,
    }
    document_token = signer.issue(
        **common, scopes={"document:read"}
    )
    callback_token = signer.issue(
        **common, scopes={"callback:write"}
    )
    plugin_config_token = signer.issue(
        **common, scopes={"plugin:load"}
    )
    plugin_token = signer.issue(
        **common,
        scopes={"command:poll", "command:result", "event:write"},
    )
    focus_origin = str(request.base_url).rstrip("/")
    plugin_query = urlencode({
        "bridge_origin": bridge_server.public_origin,
        "session_id": record.session_id,
        "access_token": plugin_token,
    })
    plugin_url = (
        f"{focus_origin}/plugins/spatial-patrol/desktop/"
        f"onlyoffice-focus-bridge/index.html?{plugin_query}"
    )
    broker.register(BridgeContext(
        session_id=record.session_id,
        document_id=record.document_id,
        plugin_url=plugin_url,
        browser_bridge_origin=bridge_server.public_origin,
        document_server_origin=runtime.snapshot.document_server_url,
    ))
    document_url = _bridge_url(
        bridge_server.container_origin,
        f"/bridge/documents/{record.session_id}/document.docx",
        document_token,
    )
    callback_url = _bridge_url(
        bridge_server.container_origin,
        f"/bridge/callback/{record.session_id}",
        callback_token,
    )
    plugin_config_url = (
        f"{bridge_server.public_origin}/bridge/plugin-config/"
        f"{record.session_id}/{plugin_config_token}/config.json"
    )
    configuration: dict[str, Any] = {
        "documentType": "word",
        "type": "desktop",
        "document": {
            "fileType": "docx",
            "key": f"{record.document_id[:32]}-{record.document_version}-{record.saved_hash[:12]}",
            "title": body.content_ref.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
            "url": document_url,
            "permissions": record.permissions,
            "info": {"owner": "Focus workspace"},
        },
        "editorConfig": {
            "callbackUrl": callback_url,
            "lang": "zh-CN",
            "mode": "edit" if body.mode == "edit" else "view",
            "customization": {
                "autosave": True,
                "forcesave": True,
                "compactHeader": False,
                "help": False,
            },
            "plugins": {
                "autostart": [BRIDGE_GUID],
                "pluginsData": [plugin_config_url],
            },
        },
        "events": {},
    }
    configuration["token"] = jwt.encode(
        configuration, runtime.jwt_secret, algorithm="HS256"
    )
    if record.status == "created":
        record = await session_manager.update(record.session_id, status="ready")
    runtime.session_opened(record.session_id)
    return {
        "session": record.to_payload(),
        "resumed": resumed,
        "document_server_url": runtime.snapshot.document_server_url,
        "config": configuration,
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    try:
        return (await session_manager.get(session_id)).to_payload()
    except SessionError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/sessions/{session_id}/force-save")
async def force_save(session_id: str) -> dict[str, Any]:
    try:
        record = await session_manager.get(session_id)
        result = await broker.issue(
            session_id,
            action="force_save",
            expected_version=record.document_version,
            timeout=30,
        )
        return {"accepted": True, "result": result}
    except (SessionError, BrokerError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/sessions/{session_id}/close")
async def close_session(session_id: str, body: CloseSession) -> dict[str, Any]:
    try:
        result = await session_manager.close(
            session_id, discard_unsaved=body.discard_unsaved
        )
        broker.unregister(session_id)
        runtime.session_closed(session_id)
        return result.to_payload()
    except SessionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except SessionError as exc:
        raise HTTPException(404, str(exc)) from exc
