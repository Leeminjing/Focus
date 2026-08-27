"""本文件提供 Document Server 与浏览器插件可达的 scope-token 桥接应用。

输入为绑定会话的签名文档请求、保存回调、编辑器命令结果与投影；输出为会话限定的
DOCX、签名 ONLYOFFICE 插件目录及命令事件。插件目录分别把加载凭证和命令凭证
保留在路径中，使 SDK 追加语言或主题 query、二次请求 ``./config.json`` 后仍可验证；
终态会话确认但吸收迟到回调；工作流不接收 ``X-Focus-Session``，也不在请求或响应中
暴露文件系统路径。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

import httpx
import jwt
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from plugins.spatial_patrol.docx_broker import BrokerError, DocxCommandBroker, broker
from plugins.spatial_patrol.docx_runtime import DocumentServerRuntime, runtime
from plugins.spatial_patrol.docx_security import ScopedTokenSigner, TokenError
from plugins.spatial_patrol.docx_sessions import (
    TERMINAL_STATUSES,
    DocxSessionManager,
    SessionError,
    session_manager,
)
from plugins.spatial_patrol.docx_storage import (
    DocxSaveError,
    atomic_save_from_url,
    normalized_origin,
)


class BridgeUnavailable(RuntimeError):
    pass


class _EmbeddedServer(uvicorn.Server):
    """Run beside Focus without taking ownership of process signals."""

    @contextmanager
    def capture_signals(self):
        yield


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandResultBody(_Strict):
    changed: bool
    document_version: int = Field(ge=1)
    target_id: str | None = None
    operation: str | None = None
    observation: dict[str, Any] | None = None
    projection: dict[str, Any] | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class EventBody(_Strict):
    event: str
    document_version: int = Field(ge=1)
    target: dict[str, Any] | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ProjectionBody(_Strict):
    target_id: str
    page: int = Field(ge=1)
    rect: dict[str, float] | None = None
    zoom: float | None = Field(default=None, gt=0)
    projection_unavailable: bool = False


def _http_404() -> HTTPException:
    return HTTPException(404, "bridge resource not found")


def _verify(
    signer: ScopedTokenSigner,
    token: str,
    scope: str,
    session_id: str,
    document_id: str | None = None,
):
    try:
        return signer.verify(
            token,
            required_scope=scope,
            session_id=session_id,
            document_id=document_id,
        )
    except TokenError as exc:
        raise _http_404() from exc


def _rewrite_save_url(url: str, public_origin: str, internal_origins: set[str]) -> str:
    source_origin = normalized_origin(url)
    public = normalized_origin(public_origin)
    if source_origin == public:
        return url
    if source_origin not in {normalized_origin(item) for item in internal_origins}:
        raise DocxSaveError("Document Server 下载 URL origin 不受信任")
    source = urlparse(url)
    destination = urlparse(public_origin)
    return urlunparse((
        destination.scheme,
        destination.netloc,
        source.path,
        source.params,
        source.query,
        "",
    ))


def callback_transition(status: int, *, dirty: bool) -> tuple[str, bool]:
    """Return (session state, requires package download)."""
    if status == 1:
        return "editing", False
    if status in {2, 6}:
        return "saving", True
    if status == 4:
        return ("recoverable" if dirty else "closed"), False
    if status in {3, 7}:
        return "recoverable", False
    return "ready", False


def _document_server_token(body: dict[str, Any], header: str | None) -> str:
    """Extract ONLYOFFICE's outgoing JWT using its body-first precedence."""
    body_token = body.get("token")
    if isinstance(body_token, str) and body_token.strip():
        return body_token.strip()
    value = (header or "").strip()
    scheme, separator, credentials = value.partition(" ")
    if separator and scheme.lower() == "bearer":
        return credentials.strip()
    return value


def create_bridge_app(
    *,
    signer: ScopedTokenSigner,
    sessions: DocxSessionManager = session_manager,
    commands: DocxCommandBroker = broker,
    ds_runtime: DocumentServerRuntime = runtime,
) -> FastAPI:
    app = FastAPI(title="Focus DOCX Bridge", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type"],
    )

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/bridge/documents/{session_id}/document.docx")
    async def document(session_id: str, access_token: str = Query(...)):
        try:
            record = await sessions.get(session_id)
            _verify(signer, access_token, "document:read", session_id, record.document_id)
            path = await sessions.resolve_path(record)
            return FileResponse(
                path,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                filename="document.docx",
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise _http_404() from exc

    @app.get("/bridge/plugin-config/{session_id}")
    @app.get("/bridge/plugin-config/{session_id}/config.json")
    async def plugin_config(
        session_id: str, request: Request, access_token: str = Query(...),
    ) -> dict:
        return _plugin_config_payload(session_id, access_token, request)

    @app.get("/bridge/plugin-config/{session_id}/{access_token}/config.json")
    async def signed_plugin_config(
        session_id: str, access_token: str, request: Request,
    ) -> dict:
        return _plugin_config_payload(session_id, access_token, request)

    def _plugin_config_payload(
        session_id: str, access_token: str, request: Request,
    ) -> dict:
        try:
            context = commands.context(session_id)
            _verify(signer, access_token, "plugin:load", session_id, context.document_id)
            command_token = parse_qs(urlparse(context.plugin_url).query).get(
                "access_token", [None]
            )[0]
            if not command_token:
                raise _http_404()
            _verify(
                signer, command_token, "command:poll", session_id, context.document_id,
            )
        except BrokerError as exc:
            raise _http_404() from exc
        base_url = (
            f"{str(request.base_url).rstrip('/')}/bridge/plugin-config/"
            f"{session_id}/{access_token}/"
        )
        return {
            "name": "Focus Bridge",
            "guid": "asc.{8F6F5C83-24D7-4DB3-9338-07384F50C764}",
            "baseUrl": base_url,
            "variations": [{
                "description": "Focus semantic target and command bridge",
                "url": f"{command_token}/index.html",
                "icons": [],
                "isViewer": True,
                "EditorsSupport": ["word"],
                "isVisual": False,
                "isModal": False,
                "isInsideMode": True,
                "initDataType": "none",
                "initOnSelectionChanged": True,
                "events": ["onDocumentContentChanged", "onTargetPositionChanged"],
            }],
        }

    def _plugin_asset_response(asset_name: str):
        media_type = {
            "index.html": "text/html",
            "code.js": "application/javascript",
        }.get(asset_name)
        if media_type is None:
            raise _http_404()
        path = (
            Path(__file__).parent
            / "desktop"
            / "onlyoffice-focus-bridge"
            / asset_name
        )
        if not path.is_file():
            raise _http_404()
        return FileResponse(path, media_type=media_type)

    @app.get("/bridge/plugin-config/{session_id}/{asset_name}")
    async def plugin_asset(session_id: str, asset_name: str):
        try:
            commands.context(session_id)
        except BrokerError as exc:
            raise _http_404() from exc
        return _plugin_asset_response(asset_name)

    @app.get("/bridge/plugin-config/{session_id}/{access_token}/{asset_name}")
    async def signed_plugin_asset(
        session_id: str, access_token: str, asset_name: str,
    ):
        try:
            context = commands.context(session_id)
            _verify(
                signer, access_token, "plugin:load", session_id, context.document_id,
            )
        except BrokerError as exc:
            raise _http_404() from exc
        return _plugin_asset_response(asset_name)

    @app.get(
        "/bridge/plugin-config/{session_id}/{load_token}/{command_token}/{asset_name}"
    )
    async def command_plugin_asset(
        session_id: str,
        load_token: str,
        command_token: str,
        asset_name: str,
        request: Request,
    ):
        try:
            context = commands.context(session_id)
            _verify(
                signer, load_token, "plugin:load", session_id, context.document_id,
            )
            _verify(
                signer, command_token, "command:poll", session_id, context.document_id,
            )
        except BrokerError as exc:
            raise _http_404() from exc
        if asset_name == "config.json":
            return _plugin_config_payload(session_id, load_token, request)
        return _plugin_asset_response(asset_name)

    @app.get("/bridge/commands/{session_id}/next")
    async def next_command(
        session_id: str,
        access_token: str = Query(...),
        timeout: float = Query(default=25, ge=0, le=25),
    ) -> dict:
        try:
            context = commands.context(session_id)
            _verify(signer, access_token, "command:poll", session_id, context.document_id)
            command = await commands.next_command(session_id, timeout)
            return {"command": command}
        except BrokerError as exc:
            raise _http_404() from exc

    @app.post("/bridge/commands/{session_id}/{command_id}/result")
    async def command_result(
        session_id: str,
        command_id: str,
        body: CommandResultBody,
        access_token: str = Query(...),
    ) -> dict[str, bool]:
        try:
            context = commands.context(session_id)
            _verify(signer, access_token, "command:result", session_id, context.document_id)
            payload = body.model_dump(exclude_none=True)
            commands.complete(command_id, payload)
            await sessions.record_edit_result(
                session_id, changed=body.changed, error=body.error
            )
            return {"ok": True}
        except (BrokerError, SessionError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/bridge/events/{session_id}")
    async def event(
        session_id: str,
        body: EventBody,
        access_token: str = Query(...),
    ) -> dict[str, bool]:
        try:
            context = commands.context(session_id)
            _verify(signer, access_token, "event:write", session_id, context.document_id)
            commands.publish_event(session_id, body.model_dump(exclude_none=True))
            if body.event in {"documentChanged", "contentChanged"}:
                await sessions.update(session_id, dirty=True, status="dirty")
            return {"ok": True}
        except (BrokerError, SessionError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/bridge/projections/{session_id}")
    async def projection(
        session_id: str,
        body: ProjectionBody,
        access_token: str = Query(...),
    ) -> dict[str, bool]:
        try:
            context = commands.context(session_id)
            _verify(signer, access_token, "projection:write", session_id, context.document_id)
            commands.publish_projection(session_id, body.model_dump(exclude_none=True))
            return {"ok": True}
        except BrokerError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/bridge/callback/{session_id}")
    async def callback(
        session_id: str,
        request: Request,
        access_token: str = Query(...),
    ) -> dict[str, int]:
        try:
            record = await sessions.get(session_id)
            _verify(signer, access_token, "callback:write", session_id, record.document_id)
            if record.status in TERMINAL_STATUSES:
                return {"error": 0}
            body = await request.json()
            ds_token = _document_server_token(
                body, request.headers.get("AuthorizationJwt")
            )
            if not ds_token:
                raise DocxSaveError("Document Server callback 缺少 JWT")
            jwt.decode(ds_token, ds_runtime.jwt_secret, algorithms=["HS256"])
            status = int(body.get("status", 0))
            if status == 1:
                await sessions.update(
                    session_id,
                    status="editing",
                    callback_status=status,
                    last_error=None,
                )
            elif status in {2, 6}:
                context = commands.context(session_id)
                save_url = _rewrite_save_url(
                    str(body.get("url") or ""),
                    context.document_server_origin,
                    set(context.document_server_internal_origins),
                )
                await sessions.update(
                    session_id, status="saving", callback_status=status,
                    recovery_url=save_url,
                )
                path = await sessions.resolve_path(record)
                saved_hash = await atomic_save_from_url(
                    path,
                    save_url,
                    expected_hash=record.saved_hash,
                    allowed_origins={context.document_server_origin},
                )
                saved = await sessions.update(
                    session_id,
                    status="saved" if status == 2 else "editing",
                    dirty=False,
                    recovery_url=None,
                    saved_hash=saved_hash,
                    opened_hash=saved_hash,
                    document_version=record.document_version + 1,
                    callback_status=status,
                    last_error=None,
                )
                commands.publish_event(session_id, {
                    "event": "diskSaved",
                    "document_version": saved.document_version,
                    "data": {"saved_hash": saved_hash, "callback_status": status},
                })
            elif status == 4:
                await sessions.update(
                    session_id,
                    status="recoverable" if record.dirty else "closed",
                    callback_status=status,
                )
            elif status in {3, 7}:
                await sessions.update(
                    session_id,
                    status="recoverable",
                    callback_status=status,
                    last_error=f"Document Server save callback status {status}",
                    recovery_url=str(body.get("url") or "") or None,
                )
            return {"error": 0}
        except Exception as exc:
            try:
                await sessions.update(
                    session_id, status="recoverable", last_error=str(exc)
                )
            except Exception:
                pass
            return {"error": 1}

    return app


class BridgeServer:
    def __init__(self, *, host: str = "0.0.0.0", port: int = 18081) -> None:
        self.host = host
        self.port = port
        self.public_origin = f"http://127.0.0.1:{port}"
        self.container_origin = f"http://host.docker.internal:{port}"
        self._lock = asyncio.Lock()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def _healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1) as client:
                response = await client.get(f"{self.public_origin}/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def prepare(self) -> None:
        async with self._lock:
            if self._task and not self._task.done() and await self._healthy():
                return
            signer = ScopedTokenSigner(runtime.bridge_secret)
            app = create_bridge_app(signer=signer)
            config = uvicorn.Config(
                app, host=self.host, port=self.port, log_level="warning", access_log=False
            )
            self._server = _EmbeddedServer(config)
            self._task = asyncio.create_task(
                self._server.serve(), name="focus-docx-bridge"
            )
            for _ in range(50):
                if await self._healthy():
                    return
                if self._task.done():
                    break
                await asyncio.sleep(0.1)
            detail = ""
            if self._task.done() and not self._task.cancelled():
                try:
                    self._task.result()
                except Exception as exc:
                    detail = f": {exc}"
            raise BridgeUnavailable(f"DOCX bridge failed to start{detail}")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True


bridge_server = BridgeServer()
