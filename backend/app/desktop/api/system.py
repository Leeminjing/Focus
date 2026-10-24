"""桌面 system HTTP 接口。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from focus.sandbox.status import sandbox_status

router = APIRouter()


@router.get("/sandbox/status")
async def get_sandbox_status() -> dict[str, object]:
    return sandbox_status()


@router.get("/bootstrap")
async def bootstrap(request: Request) -> dict:
    service = request.app.state.desktop_service
    return {
        "tasks": await service.list_tasks(),
        "equipment": await service.equipment(include_tools=False),
    }


@router.post("/reconcile")
async def reconcile_desktop_state(request: Request) -> dict:
    changes = request.app.state.desktop_service.state_changes
    changes.tasks.publish()
    changes.materials.publish()
    return {"ok": True}
