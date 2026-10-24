"""文件搜索、位置、下载和预览的 HTTP 适配。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from backend.app.desktop.workspaces import files as workspace_files
from backend.app.desktop.workspaces.files import FileTarget

file_router = APIRouter()


@file_router.get("/tasks/{task_id}/files/search")
async def search_task_files(
    task_id: str,
    request: Request,
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=30, ge=1, le=50),
):
    return await workspace_files.search_task_files(
        task_id, request.app.state.desktop_service.session_factory, q, limit
    )


@file_router.post("/files/location")
async def file_location(target: FileTarget, request: Request):
    return await workspace_files.file_location(
        target, request.app.state.desktop_service.session_factory
    )


@file_router.post("/files/content")
async def file_content(target: FileTarget, request: Request, preview: bool = False):
    data = await workspace_files.file_content(
        target, request.app.state.desktop_service.session_factory, preview
    )
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )


@file_router.post("/files/text")
async def file_text(target: FileTarget, request: Request):
    return await workspace_files.file_text(
        target, request.app.state.desktop_service.session_factory
    )


@file_router.post("/files/pdf-page")
async def file_pdf_page(target: FileTarget, request: Request, page: int = 1):
    return await workspace_files.file_pdf_page(
        target, request.app.state.desktop_service.session_factory, page
    )
