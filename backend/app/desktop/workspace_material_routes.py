"""本文件提供工作区材料 HTTP 接口与共享资源上限。

输入为已验证桌面会话、工作区身份、稳定上传提交/项身份和有界文件；输出为材料清单、上传回执、预览或删除结果。
工作流复用唯一上传与内容读取服务，精确校验 workspace owner，并在提交锁及材料行锁下保护已受理引用。
示例：POST /desktop/api/workspaces/{id}/materials/upload?submission_id=s1&item_id=0。
"""

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from backend.app.desktop.material_files import resolve_material_path
from backend.app.desktop.material_ownership import (
    ensure_material_mutable, load_material, lock_material_upload, material_upload_key,
)
from backend.app.desktop.models import DesktopMaterial, DesktopWorkspace


workspace_material_router = APIRouter()


@workspace_material_router.get("/materials/limits")
async def material_limits(request: Request) -> dict:
    return request.app.state.desktop_service.image_limits.model_dump()


@workspace_material_router.get("/workspaces/{workspace_id}")
async def workspace_details(workspace_id: str, request: Request) -> dict:
    service = request.app.state.desktop_service
    async with service.session_factory() as session:
        workspace = await _workspace(session, workspace_id)
        return service._workspace_payload(workspace)


@workspace_material_router.post("/workspaces/{workspace_id}/materials/upload")
async def upload_workspace_material(
    workspace_id: str, request: Request, file: UploadFile = File(...),
    submission_id: str = Query(min_length=1, max_length=160),
    item_id: str = Query(min_length=1, max_length=160),
) -> dict:
    service = request.app.state.desktop_service
    stored = await service.material_uploads.store_workspace(workspace_id, file, submission_id, item_id)
    return service._material_payload(stored.material, stored.workspace_path)


@workspace_material_router.get("/workspaces/{workspace_id}/materials")
async def workspace_materials(workspace_id: str, request: Request) -> list[dict]:
    service = request.app.state.desktop_service
    async with service.session_factory() as session:
        workspace = await _workspace(session, workspace_id)
        materials = await session.scalars(select(DesktopMaterial).where(
            DesktopMaterial.workspace_id == workspace_id,
        ).order_by(DesktopMaterial.created_at, DesktopMaterial.material_id))
        return [service._material_payload(material, workspace.path) for material in materials]


@workspace_material_router.get("/workspaces/{workspace_id}/materials/{material_id}/content")
async def workspace_material_content(workspace_id: str, material_id: str, request: Request) -> FileResponse:
    content = await request.app.state.desktop_service.material_contents.resolve_workspace(workspace_id, material_id)
    return FileResponse(content.path, media_type=content.media_type,
        filename=content.filename, content_disposition_type="inline")


@workspace_material_router.delete("/workspaces/{workspace_id}/materials/{material_id}")
async def delete_workspace_material(
    workspace_id: str, material_id: str, request: Request,
    submission_id: str | None = Query(default=None, min_length=1, max_length=160),
) -> dict:
    service = request.app.state.desktop_service
    async with service.session_factory.begin() as session:
        material, _ = await load_material(session, material_id, workspace_id=workspace_id)
        if submission_id is not None and (
            not material.intake_upload_key
            or material.intake_upload_key[:32] != material_upload_key(submission_id, "")[:32]
        ):
            raise HTTPException(409, "清理身份不属于这次上传")
        await lock_material_upload(session, material.intake_upload_key)
        material, workspace = await load_material(session, material_id, workspace_id=workspace_id, lock=True)
        await ensure_material_mutable(session, material)
        if material.retention == "irreplaceable":
            raise HTTPException(409, "不可遗失材料不能删除")
        try:
            path = resolve_material_path(workspace.path, material.relative_path)
        except ValueError as error:
            raise HTTPException(409, "材料路径无效") from error
        if path.is_file():
            path.unlink()
        await session.delete(material)
    return {"ok": True}


async def _workspace(session, workspace_id):
    workspace = await session.get(DesktopWorkspace, workspace_id)
    if workspace is None or not Path(workspace.path).is_dir():
        raise HTTPException(404, "工作区不存在或目录不可用")
    return workspace
