"""桌面 conversations HTTP 接口。"""

from __future__ import annotations

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from backend.app.desktop.api.schemas import SessionAccessModeUpdate
from backend.app.desktop.conversations.inputs import ConversationInputCreate

router = APIRouter()


@router.get("/tasks")
async def list_tasks(request: Request) -> list[dict]:
    return await request.app.state.desktop_service.list_tasks()


@router.get("/task-changes/stream")
async def task_changes(request: Request) -> StreamingResponse:
    signal = request.app.state.desktop_service.state_changes.tasks

    async def stream():
        generation = signal.generation
        # Always reconcile on connection/reconnection, even after missed notifications.
        yield "event: changed\ndata: {}\n\n"
        while not await request.is_disconnected():
            current = await signal.wait(generation, timeout=15)
            if current != generation:
                generation = current
                yield "event: changed\ndata: {}\n\n"
            else:
                yield ": keepalive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.get_task(task_id)


@router.get("/history/search")
async def search_history(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    from backend.app.desktop.conversations.history import HistorySearchService

    service = request.app.state.desktop_service
    return await HistorySearchService(service.session_factory, service.contexts).search(
        q, offset, limit
    )


@router.get("/history/{context_id}")
async def read_history(context_id: str, request: Request) -> dict:
    from backend.app.desktop.conversations.history import HistorySearchService

    service = request.app.state.desktop_service
    return await HistorySearchService(service.session_factory, service.contexts).read(
        context_id
    )


@router.put("/tasks/{task_id}/ui-state")
async def save_ui_state(task_id: str, body: dict, request: Request) -> dict:
    await request.app.state.desktop_service.save_ui_state(task_id, body)
    return {"ok": True}


@router.put("/tasks/{task_id}/access-mode")
async def set_session_access_mode(
    task_id: str, body: SessionAccessModeUpdate, request: Request
) -> dict:
    await request.app.state.desktop_service.set_session_access_mode(
        task_id, body.access_mode
    )
    return {"access_mode": body.access_mode}


@router.post("/tasks/{task_id}/main/inputs")
async def submit_conversation_input(
    task_id: str,
    body: "ConversationInputCreate",
    request: Request,
    idempotency_header: str = Header(
        alias="Idempotency-Key", min_length=1, max_length=160
    ),
) -> dict:
    service = request.app.state.desktop_service
    accepted = await service.conversation_inputs.submit(
        task_id, body, idempotency_header
    )
    service.notify_run_dispatch()
    return accepted


@router.delete("/tasks/{task_id}/main/inputs/{input_id}")
async def withdraw_conversation_input(
    task_id: str, input_id: str, request: Request
) -> dict:
    return await request.app.state.desktop_service.conversation_inputs.withdraw(
        task_id, input_id
    )


@router.get("/tasks/{task_id}/main/inputs")
async def list_conversation_inputs(task_id: str, request: Request) -> list[dict]:
    return await request.app.state.desktop_service.conversation_inputs.list(task_id)
