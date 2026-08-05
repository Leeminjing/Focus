"""
本文件对外提供 desktop_router，作为桌面 PoC 的 HTTP 与 SSE 接口层。

输入为带 `X-Focus-Session` 的桌面请求以及 models.py 定义的数据模型；输出为工作区、
任务、草稿、运行、材料 JSON 或独立 SSE 流。具体工作流为校验本机会话后调用
DesktopService，并保持所有事件按 run_id 订阅。示例：`app.include_router(desktop_router)`。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from backend.app.desktop.models import (
    ContinueRequest,
    DeployRequest,
    DraftUpdate,
    MainRunCreate,
    MaterialCreate,
    MaterialRestore,
    MaterialUpdate,
    ThreadCreate,
    WorkspaceCreate,
)
from focus.runtime.stream_bridge.schemas import END_SENTINEL, HEARTBEAT_SENTINEL


async def require_desktop_session(
    request: Request,
    x_focus_session: str | None = Header(default=None),
) -> None:
    supplied = x_focus_session or request.query_params.get("session")
    if not supplied or supplied != request.app.state.session_key:
        raise HTTPException(401, "无效的桌面会话")


desktop_router = APIRouter(prefix="/desktop/api", dependencies=[Depends(require_desktop_session)])


@desktop_router.get("/bootstrap")
async def bootstrap(request: Request) -> dict:
    service = request.app.state.desktop_service
    return {"tasks": await service.list_tasks(), "equipment": await service.equipment()}


@desktop_router.post("/workspaces")
async def create_workspace(body: WorkspaceCreate, request: Request) -> dict:
    return await request.app.state.desktop_service.create_workspace(body.path, body.display_name)


@desktop_router.post("/workspaces/select")
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


@desktop_router.post("/workspaces/{workspace_id}/threads")
async def create_thread(workspace_id: str, body: ThreadCreate, request: Request) -> dict:
    return await request.app.state.desktop_service.create_thread(
        workspace_id, body.thread_id, body.title
    )


@desktop_router.get("/tasks")
async def list_tasks(request: Request) -> list[dict]:
    return await request.app.state.desktop_service.list_tasks()


@desktop_router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.get_task(task_id)


@desktop_router.put("/tasks/{task_id}/ui-state")
async def save_ui_state(task_id: str, body: dict, request: Request) -> dict:
    await request.app.state.desktop_service.save_ui_state(task_id, body)
    return {"ok": True}


@desktop_router.post("/tasks/{task_id}/drafts/open")
async def open_draft(task_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.open_draft(task_id)


@desktop_router.put("/drafts/{draft_id}")
async def update_draft(draft_id: str, body: DraftUpdate, request: Request) -> dict:
    try:
        return await request.app.state.desktop_service.update_draft(draft_id, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@desktop_router.post("/drafts/{draft_id}/deploy")
async def deploy(draft_id: str, body: DeployRequest, request: Request) -> dict:
    try:
        return await request.app.state.desktop_service.deploy(draft_id, body.deployment_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@desktop_router.post("/tasks/{task_id}/main/runs")
async def start_main_run(task_id: str, body: MainRunCreate, request: Request) -> dict:
    return await request.app.state.desktop_service.start_main_run(
        task_id, body.message, body.model_name, body.permissions
    )


@desktop_router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict:
    run = await request.app.state.desktop_service.get_run(run_id)
    return request.app.state.desktop_service._run_payload(run)


@desktop_router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.cancel_run(run_id)


@desktop_router.get("/runs/{run_id}/stream")
async def stream_run(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    await request.app.state.desktop_service.get_run(run_id)
    bridge = request.app.state.desktop_service.bridge

    async def events():
        async for event in bridge.subscribe(run_id, last_event_id=last_event_id):
            if await request.is_disconnected():
                return
            if event is HEARTBEAT_SENTINEL:
                yield ": heartbeat\n\n"
            elif event is END_SENTINEL:
                yield "event: end\ndata: {}\n\n"
                return
            else:
                data = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event.id}\nevent: {event.event}\ndata: {data}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@desktop_router.get("/tasks/{task_id}/agents")
async def list_agents(task_id: str, request: Request) -> list[dict]:
    return await request.app.state.desktop_service.list_agents(task_id)


@desktop_router.get("/agents/{agent_id}/history")
async def agent_history(agent_id: str, request: Request) -> list[dict]:
    return await request.app.state.desktop_service.agent_history(agent_id)


@desktop_router.post("/agents/{agent_id}/retry")
async def retry_agent(agent_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.retry_agent(agent_id)


@desktop_router.post("/agents/{agent_id}/continue")
async def continue_agent(agent_id: str, body: ContinueRequest, request: Request) -> dict:
    return await request.app.state.desktop_service.continue_agent(agent_id, body.message)


@desktop_router.get("/tasks/{task_id}/materials")
async def list_materials(task_id: str, request: Request) -> list[dict]:
    return await request.app.state.desktop_service.list_materials(task_id)


@desktop_router.post("/tasks/{task_id}/materials")
async def enroll_material(task_id: str, body: MaterialCreate, request: Request) -> dict:
    return await request.app.state.desktop_service.enroll_material(task_id, body)


@desktop_router.post("/tasks/{task_id}/materials/upload")
async def upload_material(task_id: str, request: Request, file: UploadFile = File(...)) -> dict:
    service = request.app.state.desktop_service
    task = await service.get_task(task_id)
    filename = Path(file.filename or "upload.bin").name
    target = service._resolve_workspace_path(task["workspace_path"], filename)
    if target.exists():
        stem, suffix = target.stem, target.suffix
        target = target.with_name(f"{stem}-{uuid.uuid4().hex[:8]}{suffix}")
    target.write_bytes(await file.read())
    return await service.enroll_material(task_id, MaterialCreate(path=str(target)))


@desktop_router.put("/materials/{material_id}")
async def update_material(material_id: str, body: MaterialUpdate, request: Request) -> dict:
    return await request.app.state.desktop_service.update_material(material_id, body)


@desktop_router.post("/materials/{material_id}/clear")
async def clear_material(material_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.clear_material(material_id)


@desktop_router.delete("/materials/{material_id}")
async def delete_material(material_id: str, request: Request) -> dict:
    await request.app.state.desktop_service.delete_material(material_id)
    return {"ok": True}


@desktop_router.get("/materials/{material_id}/versions")
async def material_versions(material_id: str, request: Request) -> list[dict]:
    return await request.app.state.desktop_service.material_versions(material_id)


@desktop_router.post("/materials/{material_id}/restore")
async def restore_material(material_id: str, body: MaterialRestore, request: Request) -> dict:
    return await request.app.state.desktop_service.restore_material(material_id, body.version_id)
