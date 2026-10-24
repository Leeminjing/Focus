"""文件链接 HTTP 适配；版本与路径读取由工作区领域负责。"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from backend.app.desktop.workspaces.file_links import FileLinkReader, FileLinkReference

file_link_router = APIRouter()


@file_link_router.post("/tasks/{task_id}/file-links/content")
async def read_file_link(task_id: str, body: FileLinkReference, request: Request):
    import json
    from urllib.parse import quote

    async with request.app.state.desktop_service.session_factory() as session:
        content = await FileLinkReader().read(session, task_id, body)
    return Response(
        content=content.data,
        media_type=content.media_type,
        headers={
            "Cache-Control": "no-store",
            "X-Focus-File-Reference": quote(
                json.dumps(content.reference, ensure_ascii=False)
            ),
        },
    )
