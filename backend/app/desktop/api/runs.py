"""桌面 runs HTTP 接口。"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from backend.app.desktop.api.schemas import MainRunCreate, ResumeRequest
from backend.app.desktop.runs.intake import MainRunIntake
from backend.app.desktop.runs.orchestration import RunLauncher
from backend.app.desktop.runs.stream import durable_run_sse_consumer
from backend.app.gateway.services import start_run

router = APIRouter()


@router.get("/shell-recovery")
async def list_shell_recovery(request: Request) -> list[dict]:
    return await request.app.state.desktop_service.shell_recovery.blocked()


@router.post("/runs/{run_id}/shell-recovery")
async def recover_shell_cleanup(run_id: str, request: Request) -> dict:
    try:
        settlement = await request.app.state.desktop_service.shell_recovery.recover(
            run_id
        )
        return settlement.model_dump(mode="json")
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except (ValueError, OSError) as error:
        raise HTTPException(409, str(error)) from error


@router.post("/tasks/{task_id}/main/runs")
async def start_main_run(
    task_id: str,
    body: MainRunCreate,
    request: Request,
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    intake = MainRunIntake(
        request.app.state.desktop_service,
        getattr(request.app.state, "agent_loop_service", None),
    )
    return await intake.submit(task_id, body, idempotency_header)


@router.post("/threads/{thread_id}/runs/resume")
async def resume_run(thread_id: str, body: ResumeRequest, request: Request) -> dict:
    """承诺层人工确认恢复：以相同 thread_id resume，返回新 run 供前端订阅 SSE。"""
    if isinstance(body.resume, dict) and body.resume.get("type") == "compression":
        await request.app.state.agent_loop_service.user_resolved_compression_gate(
            thread_id
        )
    prepared = await request.app.state.desktop_service.resume_run(
        thread_id, body.resume
    )
    if prepared.agent_factory is None:
        raise HTTPException(409, "无可恢复的承诺流程")
    record = await RunLauncher(start_run).launch(prepared, request)
    if record is None:
        raise HTTPException(409, "无可恢复的承诺流程")
    return {
        "run_id": record.run_id,
        "thread_id": prepared.thread_id,
        "status": record.status.value,
    }


@router.post("/threads/{thread_id}/commitment/abandon")
async def abandon_commitment(thread_id: str, request: Request) -> dict:
    """用户显式放弃不可恢复的承诺子图，保留父图与桌面任务。"""
    return await request.app.state.desktop_service.abandon_commitment(thread_id)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.get_run_payload(run_id)


@router.get("/runs/{run_id}/causality")
async def get_run_causality(run_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.get_run_causality(run_id)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> dict:
    return await request.app.state.desktop_service.cancel_run(run_id)


@router.get("/runs/{run_id}/stream")
async def stream_run(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    service = request.app.state.desktop_service
    durable_run = await service.get_run(run_id)
    return StreamingResponse(
        durable_run_sse_consumer(service, durable_run, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
