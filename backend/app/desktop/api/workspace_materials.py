"""工作区材料 HTTP 适配；归属、冻结版本与删除事务由材料领域处理。"""

from urllib.parse import quote

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response

from backend.app.desktop.materials.workspace_service import WorkspaceMaterials

workspace_material_router = APIRouter()


@workspace_material_router.get("/materials/limits")
async def material_limits(request: Request) -> dict:
    return request.app.state.desktop_service.image_limits.model_dump()


@workspace_material_router.get("/workspaces/{workspace_id}")
async def workspace_details(workspace_id: str, request: Request) -> dict:
    return await WorkspaceMaterials(
        request.app.state.desktop_service
    ).workspace_details(workspace_id)


@workspace_material_router.post("/workspaces/{workspace_id}/materials/upload")
async def upload_workspace_material(
    workspace_id: str,
    request: Request,
    file: UploadFile = File(...),
    submission_id: str = Query(min_length=1, max_length=160),
    item_id: str = Query(min_length=1, max_length=160),
) -> dict:
    service = request.app.state.desktop_service
    stored = await service.material_uploads.store_workspace(
        workspace_id, file, submission_id, item_id
    )
    return service._material_payload(stored.material, stored.workspace_path)


@workspace_material_router.get("/workspaces/{workspace_id}/materials")
async def workspace_materials(workspace_id: str, request: Request) -> list[dict]:
    return await WorkspaceMaterials(
        request.app.state.desktop_service
    ).workspace_materials(workspace_id)


@workspace_material_router.get(
    "/workspaces/{workspace_id}/materials/{material_id}/content"
)
async def workspace_material_content(
    workspace_id: str, material_id: str, request: Request
) -> FileResponse:
    content = (
        await request.app.state.desktop_service.material_contents.resolve_workspace(
            workspace_id, material_id
        )
    )
    return FileResponse(
        content.path,
        media_type=content.media_type,
        filename=content.filename,
        content_disposition_type="inline",
    )


@workspace_material_router.get(
    "/workspaces/{workspace_id}/inputs/{intent_id}/attachments/{material_id}/content"
)
async def workspace_input_attachment(
    workspace_id: str, intent_id: str, material_id: str, request: Request
) -> Response:
    """Read the accepted input's frozen version, even when the source file changes."""
    raw, media_type, filename = await WorkspaceMaterials(
        request.app.state.desktop_service
    ).workspace_input_attachment(workspace_id, intent_id, material_id)
    return Response(
        raw,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "private, no-store",
        },
    )


@workspace_material_router.delete("/workspaces/{workspace_id}/materials/{material_id}")
async def delete_workspace_material(
    workspace_id: str,
    material_id: str,
    request: Request,
    submission_id: str | None = Query(default=None, min_length=1, max_length=160),
) -> dict:
    return await WorkspaceMaterials(
        request.app.state.desktop_service
    ).delete_workspace_material(workspace_id, material_id, submission_id)
