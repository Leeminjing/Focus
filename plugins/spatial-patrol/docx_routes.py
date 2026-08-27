"""可选 DOCX 编辑插件的 Focus 鉴权控制面。

输入为工作区文件会话、保存命令和 iframe 视口点击点；输出为隔离的编辑器配置、
会话证据与持久化语义锚点。锚点工作流先让 Bridge 把真实点击解析为页面 placement
和唯一目标，再在同一请求内提交数据库，绝不复用按钮点击前的旧光标。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from plugins.spatial_patrol.docx_broker import BridgeContext, BrokerError, broker
from plugins.spatial_patrol.docx_bridge import BridgeUnavailable, bridge_server
from plugins.spatial_patrol.docx_runtime import RuntimeUnavailable, runtime
from plugins.spatial_patrol.docx_security import ScopedTokenSigner
from plugins.spatial_patrol.docx_sessions import (
    SessionConflict,
    SessionError,
    session_manager,
)
from plugins.spatial_patrol.docx_semantic import DocxSemanticRegion, serialize_semantic_region
from plugins.spatial_patrol.models import SpatialAnchor


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


class CreateAnchor(_Strict):
    viewport_x: float = Field(ge=0)
    viewport_y: float = Field(ge=0)


def _bridge_url(origin: str, path: str, token: str) -> str:
    return f"{origin}{path}?{urlencode({'access_token': token})}"


async def _observe_anchor_target(
    command_broker, session_id: str, document_version: int, target_id: str | None,
) -> dict[str, Any]:
    if target_id:
        return command_broker.observe(session_id, target_id)
    result = await command_broker.issue(
        session_id,
        action="observe",
        expected_version=document_version,
        timeout=10,
    )
    observed = result.get("observation")
    if not isinstance(observed, dict) or not observed.get("target_id"):
        raise BrokerError("编辑器没有返回当前语义目标")
    projection = result.get("projection")
    return {**observed, "projection": projection}


async def _resolve_anchor_point(
    command_broker,
    session_id: str,
    document_version: int,
    *,
    viewport_x: float,
    viewport_y: float,
) -> dict[str, Any]:
    result = await command_broker.issue(
        session_id,
        action="resolve_anchor_point",
        arguments={"viewport_x": viewport_x, "viewport_y": viewport_y},
        expected_version=document_version,
        timeout=10,
    )
    observed = result.get("observation")
    projection = result.get("projection")
    if not isinstance(observed, dict) or not observed.get("target_id"):
        raise BrokerError("编辑器没有返回点击位置的语义结果")
    if not isinstance(projection, dict) or not isinstance(projection.get("point"), dict):
        raise BrokerError("编辑器没有返回点击位置的页面坐标")
    return {**observed, "projection": projection}


@router.get("/runtime")
async def runtime_status() -> dict[str, Any]:
    """Read-only state inspection; deliberately does not start Docker."""
    return runtime.snapshot.payload()


@router.post("/sessions")
async def create_session(body: CreateSession, request: Request) -> dict[str, Any]:
    """First DOCX open is the only path that prepares Docker and the bridge."""
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
        scopes={"command:poll", "command:result", "event:write", "projection:write"},
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


@router.get("/sessions/{session_id}/anchors")
async def session_anchors(session_id: str) -> list[dict[str, Any]]:
    try:
        record = await session_manager.get(session_id)
    except SessionError as exc:
        raise HTTPException(404, str(exc)) from exc
    from focus.persistence.engine import get_session_factory

    async with get_session_factory()() as session:
        anchors = list((await session.scalars(
            select(SpatialAnchor).where(
                SpatialAnchor.task_id == record.task_id,
                SpatialAnchor.content_ref == record.content_ref,
                SpatialAnchor.status != "dismissed",
            )
        )).all())
    payloads = [anchor.to_payload() for anchor in anchors]
    tracked = [
        {"spatial_id": item["spatial_id"], "region": item["region"]}
        for item in payloads
        if isinstance(item.get("region"), dict)
        and item["region"].get("coordinate_space") == "docx-semantic-v1"
    ]
    try:
        result = await broker.issue(
            session_id, action="subscribe_anchors", arguments={"anchors": tracked},
            expected_version=record.document_version, timeout=10,
        )
        projections = {
            item["spatial_id"]: item
            for item in result.get("evidence", {}).get("projections", [])
        }
    except BrokerError as exc:
        raise HTTPException(409, str(exc)) from exc
    for item in payloads:
        item["viewport_projection"] = projections.get(item["spatial_id"])
    return payloads


@router.post("/sessions/{session_id}/anchors")
async def create_semantic_anchor(session_id: str, body: CreateAnchor) -> dict[str, Any]:
    try:
        record = await session_manager.get(session_id)
        observed = await _resolve_anchor_point(
            broker,
            session_id,
            record.document_version,
            viewport_x=body.viewport_x,
            viewport_y=body.viewport_y,
        )
        projection = observed.get("projection") or {}
        point = projection["point"]
        target = {
            "target_id": observed["target_id"],
            "kind": observed["kind"],
            "stable_id": observed.get("stable_id"),
            "structure_path": observed.get("structure_path") or [],
            "fingerprint": observed.get("fingerprint") or {
                "exact": str(observed.get("content") or "")[:1024],
                "prefix": "",
                "suffix": "",
            },
        }
        semantic = DocxSemanticRegion.model_validate({
            "document_id": record.document_id,
            "document_version": record.document_version,
            "placement": {
                "page": projection["page"],
                "x": point["x"],
                "y": point["y"],
            },
            "attachment": projection.get("attachment"),
            "target": target,
            "projection": {
                "page": projection["page"],
                "rect": projection.get("rect"),
                "projection_unavailable": projection.get("projection_unavailable", False),
            },
        })
    except (SessionError, BrokerError, KeyError, ValueError) as exc:
        raise HTTPException(409, f"无法建立 Word 语义锚点: {exc}") from exc
    x = semantic.placement.x
    y = semantic.placement.y
    import uuid
    from focus.persistence.engine import get_session_factory

    anchor = SpatialAnchor(
        spatial_id=uuid.uuid4().hex,
        task_id=record.task_id,
        kind="anchor",
        content_ref=record.content_ref,
        page=semantic.projection.page,
        x=x,
        y=y,
        region=serialize_semantic_region(semantic),
        status="active",
        task_instruction="",
    )
    async with get_session_factory()() as session:
        session.add(anchor)
        await session.commit()
        await session.refresh(anchor)
    payload = anchor.to_payload()
    payload["viewport_projection"] = projection
    return payload


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
