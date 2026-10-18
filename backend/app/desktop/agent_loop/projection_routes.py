"""事实投影诊断与显式修复 HTTP 入口。

按当前桌面会话和 Loop 身份分页读取失败元数据，复用 FactProjector 重放隔离单元，
返回提交后的实际状态；不启动业务 Run，不把任意错误正文带入诊断响应。
"""

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from backend.app.desktop.agent_loop.fact_projector import FactProjector
from backend.app.desktop.agent_loop.live_access import LoopLiveAccessPolicy
from backend.app.desktop.agent_loop.projection_models import LoopProjectionFailure


projection_router = APIRouter(prefix="/desktop/api/agent-loops", tags=["fact-projection-diagnostics"])


def failure_view(row: LoopProjectionFailure) -> dict:
    return {
        "failure_id": row.failure_id, "loop_id": row.loop_id, "projector_name": row.projector_name,
        "unit_kind": row.unit_kind, "unit_id": row.unit_id, "status": row.status,
        "error_class": row.error_class, "attempt_count": row.attempt_count,
        "boundary_sequence": row.boundary_sequence,
        "first_failed_at": row.first_failed_at.isoformat(), "last_failed_at": row.last_failed_at.isoformat(),
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "can_repair": row.projector_name == FactProjector.PROJECTOR_NAME
        and row.status == "quarantined" and row.unit_kind in {"journal_event", "run_backfill"},
    }


@projection_router.get("/{loop_id}/projection-failures")
async def projection_failures(
    loop_id: str, request: Request,
    include_resolved: bool = False,
    before: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=40, ge=1, le=100),
) -> dict:
    async with request.app.state.desktop_service.session_factory() as session:
        await LoopLiveAccessPolicy().resolve(session, loop_id)
        query = select(LoopProjectionFailure).where(LoopProjectionFailure.loop_id == loop_id)
        if not include_resolved:
            query = query.where(LoopProjectionFailure.status != "resolved")
        if before:
            query = query.where(LoopProjectionFailure.failure_id < before)
        rows = tuple(await session.scalars(query.order_by(LoopProjectionFailure.failure_id.desc()).limit(limit + 1)))
        page = rows[:limit]
        return {"loop_id": loop_id, "items": [failure_view(row) for row in page],
                "has_more": len(rows) > limit,
                "next_before": page[-1].failure_id if len(rows) > limit else None}


@projection_router.post("/{loop_id}/projection-failures/{failure_id}/repair")
async def repair_projection_failure(loop_id: str, failure_id: str, request: Request) -> dict:
    sessions = request.app.state.desktop_service.session_factory
    async with sessions() as session:
        await LoopLiveAccessPolicy().resolve(session, loop_id)
        row = await session.get(LoopProjectionFailure, failure_id)
        if row is None or row.loop_id != loop_id:
            raise HTTPException(404, "事实投影失败记录不存在")
        if not failure_view(row)["can_repair"] and not (
            row.projector_name == FactProjector.PROJECTOR_NAME and row.status == "resolved"
        ):
            raise HTTPException(409, "仅隔离的事实投影可修复重放")
    projector = getattr(request.app.state, "agent_loop_fact_projector", None)
    if projector is None:
        raise HTTPException(503, "事实投影修复服务尚未就绪")
    try:
        processed = await projector.repair_failure(failure_id, loop_id=loop_id)
    except LookupError as error:
        raise HTTPException(404, "事实投影失败记录不存在") from error
    except ValueError as error:
        raise HTTPException(409, "当前记录不支持事实投影修复") from error
    async with sessions() as session:
        row = await session.get(LoopProjectionFailure, failure_id)
        if row is None or row.loop_id != loop_id:
            raise HTTPException(404, "事实投影失败记录不存在")
        return {"loop_id": loop_id, "processed": processed, "failure": failure_view(row)}
