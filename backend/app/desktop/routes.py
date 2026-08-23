"""
本文件对外提供 desktop_router，作为桌面 PoC 的 HTTP 与 SSE 接口层。

输入为带 `X-Focus-Session` 的桌面请求以及 models.py 定义的数据模型；输出为工作区、
Context、任务、草稿、运行、材料 JSON 或独立 SSE 流。具体工作流为校验本机会话后调用
DesktopService，并保持所有事件按 run_id 订阅。示例：`app.include_router(desktop_router)`。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import uuid

from fastapi import APIRouter, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from backend.app.desktop.models import (
    ContinueRequest,
    ContextDefinitionUpdate,
    ContextDeriveCreate,
    ContextProjectionDecision,
    DeployRequest,
    DraftUpdate,
    MainRunCreate,
    MaterialCreate,
    MaterialRestore,
    MaterialUpdate,
    ResumeRequest,
    ThreadCreate,
    WorkspaceCreate,
)
from backend.app.desktop.service import PreparedRun
from backend.app.gateway.routers.thread_runs import sse_consumer
from backend.app.gateway.services import start_run


# 决策 10：桌面 API 只接受 loopback 对等连接（含 host_command 真实宿主机命令）。
# 保护由 AuthMiddleware（统一会话保护）统一承担，本路由不再挂独立依赖。
desktop_router = APIRouter(prefix="/desktop/api")


@desktop_router.get("/bootstrap")
async def bootstrap(request: Request) -> dict:
    service = request.app.state.desktop_service
    from focus.plugins import get_plugin_registry

    plugins = [
        plugin for plugin in get_plugin_registry().list_plugins()
        if plugin["status"] == "active"
    ]
    return {
        "tasks": await service.list_tasks(),
        "equipment": await service.equipment(),
        "plugins": plugins,
    }


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


@desktop_router.get("/contexts/{context_id}/snapshot")
async def get_context_snapshot(
    context_id: str, request: Request, checkpoint_id: str | None = None
) -> dict:
    return await request.app.state.desktop_service.contexts.snapshot(context_id, checkpoint_id)


@desktop_router.get("/contexts/{context_id}/lineage")
async def get_context_lineage(context_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.contexts.lineage(context_id)


@desktop_router.get("/workspaces/{workspace_id}/contexts/tree")
async def get_context_tree(workspace_id: str, request: Request) -> list[dict]:
    return await request.app.state.desktop_service.contexts.tree(workspace_id)


@desktop_router.post("/contexts/derive")
async def derive_context(body: ContextDeriveCreate, request: Request) -> dict:
    return await request.app.state.desktop_service.contexts.derive(body)


@desktop_router.put("/contexts/{context_id}/definition")
async def update_context_definition(
    context_id: str, body: ContextDefinitionUpdate, request: Request
) -> dict:
    return await request.app.state.desktop_service.contexts.update_definition(context_id, body)


@desktop_router.post("/contexts/{context_id}/projection/decision")
async def decide_context_projection(
    context_id: str, body: ContextProjectionDecision, request: Request
) -> dict:
    return await request.app.state.desktop_service.contexts.decide(context_id, body)


@desktop_router.post("/contexts/{context_id}/archive")
async def archive_context(
    context_id: str, request: Request, cascade: bool = Query(default=False)
) -> dict:
    return await request.app.state.desktop_service.contexts.archive(context_id, cascade)


@desktop_router.post("/contexts/{context_id}/unarchive")
async def unarchive_context(context_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.contexts.unarchive(context_id)


@desktop_router.delete("/contexts/{context_id}")
async def delete_context(
    context_id: str, request: Request, cascade: bool = Query(default=False)
) -> dict:
    return await request.app.state.desktop_service.contexts.delete(context_id, cascade)


@desktop_router.get("/sessions/archived")
async def list_archived_sessions(request: Request) -> list[dict]:
    return await request.app.state.desktop_service.contexts.list_archived()


@desktop_router.get("/tasks/{task_id}/skills")
async def list_task_skills(task_id: str, request: Request) -> dict:
    return {"skills": await request.app.state.desktop_service.list_task_skills(task_id)}


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


async def _launch(request: Request, prepared: PreparedRun | None) -> None:
    """桌面运行接口的统一发起入口：委托 services.start_run 创建并执行 run，并挂载 DB 终态同步。

    输入:
        request: Request — FastAPI 请求（提供 app.state 资源与 current_user）
        prepared: PreparedRun | None — 编排输入；None 表示幂等命中已有 run，无需发起
    """
    if prepared is None or prepared.agent_factory is None:
        return  # 幂等命中已有 run，无需发起
    record = await start_run(
        prepared.body, prepared.thread_id, request, agent_factory=prepared.agent_factory
    )
    request.app.state.desktop_service.attach_run_sync(record)


@desktop_router.post("/drafts/{draft_id}/deploy")
async def deploy(draft_id: str, body: DeployRequest, request: Request) -> dict:
    try:
        prepared = await request.app.state.desktop_service.deploy(draft_id, body.deployment_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await _launch(request, prepared)
    return prepared.payload


@desktop_router.post("/tasks/{task_id}/main/runs")
async def start_main_run(task_id: str, body: MainRunCreate, request: Request) -> dict:
    prepared = await request.app.state.desktop_service.start_main_run(
        task_id, body.message, body.model_name, body.permissions, body.skills,
        body.spatial_focus,
    )
    await _launch(request, prepared)
    return prepared.payload


@desktop_router.get("/assembly/task")
async def get_assembly_task(request: Request) -> dict:
    """确保「无工作区模式」保留工作区与其任务存在，返回该任务 payload（复用任务页全套能力）。"""
    return await request.app.state.desktop_service.ensure_assembly_task()


@desktop_router.post("/threads/{thread_id}/runs/resume")
async def resume_run(thread_id: str, body: ResumeRequest, request: Request) -> dict:
    """承诺层人工确认恢复：以相同 thread_id resume，返回新 run 供前端订阅 SSE。"""
    prepared = await request.app.state.desktop_service.resume_run(thread_id, body.resume)
    if prepared.agent_factory is None:
        raise HTTPException(409, "无可恢复的承诺流程")
    record = await start_run(
        prepared.body, prepared.thread_id, request, agent_factory=prepared.agent_factory
    )
    request.app.state.desktop_service.attach_run_sync(record)
    return {
        "run_id": record.run_id,
        "thread_id": prepared.thread_id,
        "status": record.status.value,
    }


@desktop_router.post("/threads/{thread_id}/commitment/abandon")
async def abandon_commitment(thread_id: str, request: Request) -> dict:
    """用户显式放弃不可恢复的承诺子图，保留父图与桌面任务。"""
    return await request.app.state.desktop_service.abandon_commitment(thread_id)


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
    # 统一 SSE 消费：复用 thread_runs.sse_consumer（信封格式帧，断线重连/心跳）
    service = request.app.state.desktop_service
    await service.get_run(run_id)
    record = service.run_manager.get(run_id)
    if record is None:
        raise HTTPException(404, "运行不存在（流已过期）")
    return StreamingResponse(
        sse_consumer(service.bridge, record, request, service.run_manager),
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
    prepared = await request.app.state.desktop_service.retry_agent(agent_id)
    await _launch(request, prepared)
    return prepared.payload


@desktop_router.post("/agents/{agent_id}/continue")
async def continue_agent(agent_id: str, body: ContinueRequest, request: Request) -> dict:
    prepared = await request.app.state.desktop_service.continue_agent(agent_id, body.message)
    await _launch(request, prepared)
    return prepared.payload


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
