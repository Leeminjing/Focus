"""把持久 DesktopRun 桥接到稍后才出现的进程内 SSE RunRecord。"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from backend.app.gateway.routers.thread_runs import format_sse, sse_consumer


_TERMINAL_STATUSES = frozenset({"success", "error", "interrupted", "timeout"})
_REGISTRATION_POLL_SECONDS = 0.1
_HEARTBEAT_SECONDS = 15.0


async def durable_run_sse_consumer(
    service: Any,
    durable_run: Any,
    request: Any,
) -> AsyncIterator[str]:
    """保持 SSE 为 200，直到持久 Run 进入实时注册表或直接到达终态。"""
    run_id = str(durable_run.run_id)
    current = durable_run
    next_heartbeat = 0.0

    while True:
        record = service.run_manager.get(run_id)
        if record is not None:
            async for frame in sse_consumer(
                service.bridge,
                record,
                request,
                service.run_manager,
            ):
                yield frame
            return

        status = str(current.status)
        if status in _TERMINAL_STATUSES:
            yield format_sse(
                "end",
                {
                    "run_id": run_id,
                    "status": status,
                    "error": current.error,
                },
                "end",
            )
            return

        if await request.is_disconnected():
            return

        now = asyncio.get_running_loop().time()
        if now >= next_heartbeat:
            # 首帧确保 EventSource 建连成功；后续低频保活，不把调度轮询暴露成网络噪声。
            yield ": awaiting-run-registration\n\n"
            next_heartbeat = now + _HEARTBEAT_SECONDS
        await asyncio.sleep(_REGISTRATION_POLL_SECONDS)
        current = await service.get_run(run_id)
