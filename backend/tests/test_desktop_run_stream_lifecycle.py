"""Desktop SSE 必须以持久 Run 为入口，并跨过异步调度到进程内运行态的时间窗。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import HTTPException

from backend.app.desktop.routes import stream_run
from focus.runtime.runs.manager import RunManager
from focus.runtime.runs.schemas import RunStatus
from focus.runtime.stream_bridge.memory import MemoryStreamBridge


class _DurableRunService:
    def __init__(self, status: str = "pending") -> None:
        self.status = status
        self.error: str | None = None
        self.run_manager = RunManager()
        self.bridge = MemoryStreamBridge()

    async def get_run(self, run_id: str):
        if run_id == "missing":
            raise HTTPException(404, "运行不存在")
        return SimpleNamespace(run_id=run_id, status=self.status, error=self.error)


class _Request:
    def __init__(self, service: _DurableRunService) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(desktop_service=service))
        self.headers: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


async def _body(response) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def test_stream_waits_for_durable_run_to_enter_live_registry() -> None:
    async def scenario() -> str:
        service = _DurableRunService()
        request = _Request(service)

        async def register_live_run() -> None:
            await asyncio.sleep(0)
            record = service.run_manager.create("thread-1", run_id="run-1")
            record.status = RunStatus.success
            service.status = "success"
            service.bridge.publish_end(record.run_id)

        registration = asyncio.create_task(register_live_run())
        response = await stream_run("run-1", request)
        body = await asyncio.wait_for(_body(response), timeout=1)
        await registration
        return body

    body = asyncio.run(scenario())

    assert "event: end" in body
    assert '"status": "success"' in body


def test_stream_closes_from_durable_terminal_state_after_live_cleanup() -> None:
    async def scenario() -> str:
        service = _DurableRunService(status="success")
        response = await stream_run("run-complete", _Request(service))
        return await asyncio.wait_for(_body(response), timeout=1)

    body = asyncio.run(scenario())

    assert "event: end" in body
    assert '"run_id": "run-complete"' in body
    assert '"status": "success"' in body


def test_stream_still_rejects_unknown_run() -> None:
    async def scenario() -> None:
        service = _DurableRunService()
        try:
            await stream_run("missing", _Request(service))
        except HTTPException as exc:
            assert exc.status_code == 404
            return
        raise AssertionError("未知 Run 必须返回 404")

    asyncio.run(scenario())
