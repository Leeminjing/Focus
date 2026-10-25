"""Session-authenticated Context browser management and live snapshots."""
import json
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from .transport import BrowserError

browser_router = APIRouter()

class BrowserBindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_context_id: str | None = None

class BrowserPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    page_id: str | None = Field(default=None, min_length=1, max_length=128)

@browser_router.get("/contexts/{context_id}/browser/pages")
async def browser_pages(context_id: str, request: Request):
    try:
        return await request.app.state.desktop_service.browsers.pages(context_id)
    except BrowserError as error:
        raise HTTPException(409, error.payload()) from error

@browser_router.get("/contexts/{context_id}/browser/downloads")
async def browser_downloads(context_id: str, request: Request):
    try:
        return await request.app.state.desktop_service.browsers.downloads(context_id)
    except BrowserError as error:
        raise HTTPException(409, error.payload()) from error

@browser_router.get("/contexts/{context_id}/browser")
async def browser_status(context_id: str, request: Request):
    return await request.app.state.desktop_service.browsers.snapshot(context_id)

@browser_router.post("/contexts/{context_id}/browser/present")
async def present_browser(context_id: str, request: Request, body: BrowserPresentation | None = None):
    try:
        return await request.app.state.desktop_service.browsers.present(context_id, body)
    except BrowserError as error:
        raise HTTPException(409, error.payload()) from error

@browser_router.post('/contexts/{context_id}/browser/recover')
async def recover_browser(context_id: str, request: Request):
    try:
        return await request.app.state.desktop_service.browsers.recover(context_id)
    except BrowserError as error:
        raise HTTPException(409, error.payload()) from error

@browser_router.put("/contexts/{context_id}/browser")
async def bind_browser(context_id: str, body: BrowserBindingUpdate, request: Request):
    try:
        return await request.app.state.desktop_service.browsers.replace(context_id, body.source_context_id)
    except BrowserError as error:
        raise HTTPException(409, error.payload()) from error

@browser_router.get("/contexts/{context_id}/browser/stream")
async def browser_stream(context_id: str, request: Request):
    browsers = request.app.state.desktop_service.browsers
    await browsers.snapshot(context_id)
    async def snapshots():
        while not await request.is_disconnected():
            generation = browsers.changes.generation
            data = json.dumps(await browsers.snapshot(context_id), ensure_ascii=False)
            yield f"event: browser\ndata: {data}\n\n"
            await browsers.changes.wait(generation, timeout=30)
    return StreamingResponse(snapshots(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
