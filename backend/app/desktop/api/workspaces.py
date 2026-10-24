"""桌面 workspaces HTTP 接口。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from backend.app.desktop.api.schemas import ThreadCreate, WorkspaceCreate

router = APIRouter()


@router.post("/workspaces")
async def create_workspace(body: WorkspaceCreate, request: Request) -> dict:
    return await request.app.state.desktop_service.create_workspace(
        body.path, body.display_name
    )


@router.post("/workspaces/select")
async def select_workspace() -> dict:
    def choose() -> str:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return filedialog.askdirectory() or ""
        finally:
            root.destroy()

    path = await asyncio.to_thread(choose)
    return {"path": path}


@router.post("/workspaces/{workspace_id}/threads")
async def create_thread(
    workspace_id: str, body: ThreadCreate, request: Request
) -> dict:
    return await request.app.state.desktop_service.create_thread(
        workspace_id, body.thread_id, body.title, body.access_mode
    )


@router.get("/assembly/task")
async def get_assembly_task(request: Request, workspace_id: str | None = None) -> dict:
    """确保「无工作区模式」保留工作区与其任务存在，返回该任务 payload（复用任务页全套能力）。"""
    return await request.app.state.desktop_service.ensure_assembly_task(workspace_id)
