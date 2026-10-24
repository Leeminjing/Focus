"""桌面 patrol HTTP 接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from backend.app.desktop.api.launch import _launch
from backend.app.desktop.api.schemas import (
    ContinueRequest,
    DeployRequest,
    DraftUpdate,
    ResumeRequest,
)
from backend.app.desktop.patrol.session_patrol.contracts import SourcePreviewRequest

router = APIRouter()


@router.post("/tasks/{task_id}/drafts/open")
async def open_draft(task_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.open_draft(task_id)


@router.put("/drafts/{draft_id}")
async def update_draft(draft_id: str, body: DraftUpdate, request: Request) -> dict:
    try:
        return await request.app.state.desktop_service.update_draft(draft_id, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/drafts/{draft_id}/deploy")
async def deploy(draft_id: str, body: DeployRequest, request: Request) -> dict:
    try:
        prepared = await request.app.state.desktop_service.deploy(
            draft_id, body.deployment_id, body.preview_token
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await _launch(request, prepared)
    return prepared.payload


@router.post("/tasks/{task_id}/context-curation/quick-deploy")
async def quick_deploy_context_curator(
    task_id: str, body: DeployRequest, request: Request
) -> dict:
    try:
        prepared = await request.app.state.desktop_service.quick_deploy_context_curator(
            task_id, body.deployment_id
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await _launch(request, prepared)
    return prepared.payload


@router.post("/drafts/{draft_id}/preview")
async def preview_patrol(draft_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.session_patrol.preview(draft_id)


@router.get("/patrol/sources")
async def patrol_source_catalog(
    request: Request,
    kind: str = "context",
    query: str = Query(default="", max_length=1000),
    cursor: str | None = Query(default=None, max_length=4096),
    limit: int | None = Query(default=None, ge=1, le=100),
) -> dict:
    return await request.app.state.desktop_service.patrol_sources.catalog(
        kind=kind, query=query, cursor=cursor, limit=limit
    )


@router.post("/drafts/{draft_id}/source-preview")
async def preview_patrol_source(
    draft_id: str, body: SourcePreviewRequest, request: Request
) -> dict:
    return await request.app.state.desktop_service.patrol_sources.preview(
        draft_id, body.model_dump(mode="json", exclude_none=True)
    )


@router.get("/drafts/{draft_id}/source-status")
async def patrol_source_status(draft_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.session_patrol.source_status(
        draft_id
    )


@router.post("/drafts/{draft_id}/sources")
async def import_patrol_source(draft_id: str, body: dict, request: Request) -> dict:
    try:
        return await request.app.state.desktop_service.patrol_sources.import_into(
            draft_id, body
        )
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(
            422, {"code": "source_unavailable", "message": str(exc)}
        ) from exc


@router.post("/runs/{run_id}/patrol-resume")
async def resume_patrol(run_id: str, body: ResumeRequest, request: Request) -> dict:
    prepared = await request.app.state.desktop_service.session_patrol.resume(
        run_id, body.resume
    )
    await _launch(request, prepared)
    return prepared.payload


@router.get("/agents/{agent_id}/context-curation")
async def context_curation_detail(
    agent_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    before: str | None = None,
) -> dict:
    return await request.app.state.desktop_service.context_patrol.detail(
        agent_id, limit=limit, before=before
    )


@router.post("/agents/{agent_id}/retry")
async def retry_agent(agent_id: str, request: Request) -> dict:
    prepared = await request.app.state.desktop_service.retry_agent(agent_id)
    await _launch(request, prepared)
    return prepared.payload


@router.post("/agents/{agent_id}/drafts/open")
async def edit_patrol_definition(agent_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.session_patrol.edit_definition(
        agent_id
    )


@router.get("/runs/{run_id}/patrol-definition")
async def patrol_definition_audit(run_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.session_patrol.audit(run_id)


@router.get("/drafts/{draft_id}")
async def read_patrol_draft(draft_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.session_patrol.read_draft(draft_id)


@router.post("/agents/{agent_id}/continue")
async def continue_agent(
    agent_id: str, body: ContinueRequest, request: Request
) -> dict:
    prepared = await request.app.state.desktop_service.continue_agent(
        agent_id, body.message, body.run_id, body.checkpoint_id
    )
    await _launch(request, prepared)
    return prepared.payload
