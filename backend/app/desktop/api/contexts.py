"""桌面 contexts HTTP 接口。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from backend.app.desktop.api.schemas import (
    BatchDeleteRequest,
    ContextTrackingUpdate,
)

router = APIRouter()


@router.get("/contexts/{context_id}/snapshot")
async def get_context_snapshot(
    context_id: str,
    request: Request,
    checkpoint_id: str | None = Query(default=None, description="仅校验当前 checkpoint，不读取历史版本"),
) -> dict:
    return await request.app.state.desktop_service.contexts.snapshot(
        context_id, checkpoint_id
    )


@router.get("/contexts/{context_id}/lineage")
async def get_context_lineage(context_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.contexts.lineage(context_id)


@router.get("/workspaces/{workspace_id}/contexts/tree")
async def get_context_tree(workspace_id: str, request: Request) -> list[dict]:
    return await request.app.state.desktop_service.contexts.tree(workspace_id)


@router.post("/contexts/{context_id}/archive")
async def archive_context(
    context_id: str, request: Request, cascade: bool = Query(default=False)
) -> dict:
    return await request.app.state.desktop_service.contexts.archive(context_id, cascade)


@router.post("/contexts/{context_id}/unarchive")
async def unarchive_context(context_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.contexts.unarchive(context_id)


@router.delete("/contexts/{context_id}")
async def delete_context(
    context_id: str, request: Request, cascade: bool = Query(default=False)
) -> dict:
    return await request.app.state.desktop_service.contexts.delete(context_id, cascade)


@router.post("/contexts/batch-delete")
async def batch_delete_contexts(body: BatchDeleteRequest, request: Request) -> dict:
    return await request.app.state.desktop_service.contexts.delete_many(
        body.context_ids, body.cascade
    )


@router.get("/sessions/archived")
async def list_archived_sessions(request: Request) -> list[dict]:
    return await request.app.state.desktop_service.contexts.list_archived()


@router.put("/agents/{agent_id}/context-curation/state")
async def update_context_curation_state(
    agent_id: str, body: ContextTrackingUpdate, request: Request
) -> dict:
    return await request.app.state.desktop_service.context_patrol.set_tracking_state(
        agent_id, body.state
    )
