"""桌面 materials HTTP 接口。"""

from __future__ import annotations

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse

from backend.app.desktop.api.schemas import (
    MaterialCreate,
    MaterialGroupCreate,
    MaterialGroupOrder,
    MaterialGroupUpdate,
    MaterialMembershipOrder,
    MaterialMembershipUpdate,
    MaterialRestore,
    MaterialUpdate,
)

router = APIRouter()


@router.get("/tasks/{task_id}/materials")
async def list_materials(task_id: str, request: Request) -> list[dict]:
    return await request.app.state.desktop_service.list_materials(task_id)


@router.get("/tasks/{task_id}/material-history")
async def list_material_history(
    task_id: str, request: Request, material_id: str | None = Query(default=None)
) -> list[dict]:
    return await request.app.state.desktop_service.list_material_history(
        task_id, material_id
    )


@router.get("/tasks/{task_id}/material-groups")
async def list_material_groups(task_id: str, request: Request) -> list[dict]:
    return await request.app.state.desktop_service.material_groups.list(task_id)


@router.post("/tasks/{task_id}/material-groups")
async def create_material_group(
    task_id: str, body: MaterialGroupCreate, request: Request
) -> dict:
    return await request.app.state.desktop_service.material_groups.create(
        task_id, body.name
    )


@router.put("/tasks/{task_id}/material-groups/order")
async def reorder_material_groups(
    task_id: str, body: MaterialGroupOrder, request: Request
) -> list[dict]:
    return await request.app.state.desktop_service.material_groups.reorder(
        task_id, body.group_ids
    )


@router.put("/tasks/{task_id}/material-groups/{group_id}")
async def rename_material_group(
    task_id: str, group_id: str, body: MaterialGroupUpdate, request: Request
) -> dict:
    return await request.app.state.desktop_service.material_groups.rename(
        task_id, group_id, body.name
    )


@router.delete("/tasks/{task_id}/material-groups/{group_id}")
async def delete_material_group(
    task_id: str, group_id: str, request: Request, confirm: bool = Query(default=False)
) -> dict:
    await request.app.state.desktop_service.material_groups.delete(
        task_id, group_id, confirm
    )
    return {"ok": True}


@router.put("/tasks/{task_id}/materials/{material_id}/group")
async def move_material_to_group(
    task_id: str, material_id: str, body: MaterialMembershipUpdate, request: Request
) -> dict:
    membership = await request.app.state.desktop_service.material_groups.move(
        task_id, material_id, body.group_id, body.position
    )
    return {"membership": membership}


@router.put("/tasks/{task_id}/material-groups/{group_id}/materials/order")
async def reorder_material_group_members(
    task_id: str, group_id: str, body: MaterialMembershipOrder, request: Request
) -> list[dict]:
    return await request.app.state.desktop_service.material_groups.reorder_members(
        task_id, group_id, body.material_ids
    )


@router.post("/tasks/{task_id}/materials")
async def enroll_material(task_id: str, body: MaterialCreate, request: Request) -> dict:
    return await request.app.state.desktop_service.enroll_material(task_id, body)


@router.post("/tasks/{task_id}/materials/upload")
async def upload_material(
    task_id: str, request: Request, file: UploadFile = File(...)
) -> dict:
    service = request.app.state.desktop_service
    return await service.store_uploaded_material(task_id, file)


@router.get("/tasks/{task_id}/materials/{material_id}/content")
async def material_content(
    task_id: str, material_id: str, request: Request
) -> FileResponse:
    content = await request.app.state.desktop_service.resolve_material_content(
        task_id, material_id
    )
    return FileResponse(
        path=content.path,
        media_type=content.media_type,
        filename=content.filename,
        content_disposition_type="inline",
    )


@router.get("/tasks/{task_id}/materials/{material_id}/image-validity")
async def image_material_validity(
    task_id: str, material_id: str, request: Request
) -> dict:
    return await request.app.state.desktop_service.validate_image_material(
        task_id, material_id
    )


@router.put("/materials/{material_id}")
async def update_material(
    material_id: str, body: MaterialUpdate, request: Request
) -> dict:
    return await request.app.state.desktop_service.update_material(material_id, body)


@router.post("/materials/{material_id}/clear")
async def clear_material(material_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.clear_material(material_id)


@router.delete("/materials/{material_id}")
async def delete_material(material_id: str, request: Request) -> dict:
    await request.app.state.desktop_service.delete_material(material_id)
    return {"ok": True}


@router.get("/materials/{material_id}/versions")
async def material_versions(material_id: str, request: Request) -> list[dict]:
    return await request.app.state.desktop_service.material_versions(material_id)


@router.post("/materials/{material_id}/restore")
async def restore_material(
    material_id: str, body: MaterialRestore, request: Request
) -> dict:
    return await request.app.state.desktop_service.restore_material(
        material_id, body.version_id
    )
