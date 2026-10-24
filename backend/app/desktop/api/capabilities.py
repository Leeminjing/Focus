"""桌面 capabilities HTTP 接口。"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/equipment")
async def get_equipment(request: Request) -> dict:
    return await request.app.state.desktop_service.equipment()


@router.get("/tasks/{task_id}/skills")
async def list_task_skills(task_id: str, request: Request) -> dict:
    return {"skills": await request.app.state.desktop_service.list_task_skills(task_id)}


@router.post("/tasks/{task_id}/capabilities/refresh")
async def refresh_task_capabilities(task_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.refresh_task_capabilities(task_id)
