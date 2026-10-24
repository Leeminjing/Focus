"""桌面 agents HTTP 接口。"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/tasks/{task_id}/agents")
async def list_agents(task_id: str, request: Request) -> list[dict]:
    return await request.app.state.desktop_service.list_agents(task_id)


@router.get("/agents/{agent_id}/history")
async def agent_history(agent_id: str, request: Request) -> list[dict]:
    return await request.app.state.desktop_service.agent_history(agent_id)
