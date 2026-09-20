r"""本文件对外提供 live_loop_router 的 Live Snapshot 与 resumable SSE 传输边界。

输入为 Loop id、`after_sequence`、桌面会话和断连信号；输出为完整 Live projection 或 multiplexed SSE 事件、heartbeat、
snapshot-required/resync-required 控制帧。具体工作流为 snapshot 在短事务内生成，stream 逐批读取且不维护无界内存队列，
断连即停止轮询。示例：`app.include_router(live_loop_router)`。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from backend.app.desktop.agent_loop.live_api import LoopLiveEventFeed, LoopLiveSnapshotService
from backend.app.desktop.agent_loop.feature_flags import LoopFeatureFlags


live_loop_router = APIRouter(prefix="/desktop/api/agent-loops", tags=["agent-loop-live"])


@live_loop_router.get("/{loop_id}/live")
async def live_loop_snapshot(loop_id: str, request: Request) -> dict:
    _require_live_api(request)
    sessions = request.app.state.desktop_service.session_factory
    async with sessions.begin() as session:
        return await LoopLiveSnapshotService().read(session, loop_id)


@live_loop_router.get("/{loop_id}/live/stream")
async def live_loop_stream(loop_id: str, request: Request, after_sequence: int = Query(default=0, ge=0)) -> StreamingResponse:
    _require_live_api(request)
    feed = LoopLiveEventFeed(request.app.state.desktop_service.session_factory)

    async def stream():
        cursor = after_sequence
        idle_ticks = 0
        while not await request.is_disconnected():
            batch = await feed.read_batch(loop_id, cursor)
            if batch["status"] != "events":
                yield _sse(batch["status"], batch, cursor)
                return
            events = batch["events"]
            if events:
                idle_ticks = 0
                for event in events:
                    cursor = max(cursor, int(event["sequence"]))
                    yield _sse(event["kind"], event, cursor)
            else:
                idle_ticks += 1
                if idle_ticks >= 30:
                    idle_ticks = 0
                    yield f": sequence={cursor}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(kind: str, payload: dict, sequence: int) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {sequence}\nevent: {kind}\ndata: {body}\n\n"


def _require_live_api(request: Request) -> None:
    flags = getattr(request.app.state, "loop_feature_flags", None) or LoopFeatureFlags()
    if not flags.live_api:
        from fastapi import HTTPException
        raise HTTPException(404, "Live Loop API 未启用")
