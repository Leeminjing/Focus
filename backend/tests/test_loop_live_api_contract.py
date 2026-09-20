r"""本文件验证 Live Loop HTTP/SSE 边界对 malformed cursor、断连与控制帧编码的稳定行为。

输入为最小 FastAPI router、负 cursor、已断连 request、授权受限事件和协议 payload；输出为 422 参数拒绝、零输出断连流、
同 sequence 脱敏占位信封与合法 SSE 帧。具体工作流为使用独立测试应用触发框架校验，再验证授权策略不会制造 cursor gap。
示例：`pytest test_loop_live_api_contract.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.desktop.agent_loop.live_routes import _sse, live_loop_router, live_loop_stream
from backend.app.desktop.agent_loop.event_contract import CanonicalEventEnvelope, EventVisibility, LiveEventAuthorizationPolicy


def test_live_stream_rejects_malformed_cursor() -> None:
    app = FastAPI()
    app.include_router(live_loop_router)
    with TestClient(app) as client:
        response = client.get("/desktop/api/agent-loops/loop/live/stream?after_sequence=-1")
    assert response.status_code == 422


def test_live_stream_stops_without_output_after_disconnect() -> None:
    class Request:
        app = SimpleNamespace(state=SimpleNamespace(desktop_service=SimpleNamespace(session_factory=None)))

        async def is_disconnected(self) -> bool:
            return True

    async def run() -> None:
        response = await live_loop_stream("loop", Request(), 0)
        assert [chunk async for chunk in response.body_iterator] == []

    asyncio.run(run())


def test_live_sse_frame_preserves_sequence_kind_and_json() -> None:
    frame = _sse("fact.upserted", {"sequence": 7, "state": "verified"}, 7)
    assert frame.startswith("id: 7\nevent: fact.upserted\ndata: ")
    assert '"state":"verified"' in frame


def test_unauthorized_event_advances_sequence_without_leaking_identity() -> None:
    event = CanonicalEventEnvelope(
        event_id="secret-event",
        loop_id="loop",
        sequence=9,
        schema_version=1,
        kind="context.tool.completed",
        entity_type="tool",
        entity_id="private-tool-call",
        entity_revision=2,
        correlation_id="private-directive",
        causation_id="private-cause",
        visibility=EventVisibility(required_permissions=("view_evidence",)),
        payload={"summary": "private output", "secret": "never expose"},
        idempotency_key="private-key",
        occurred_at=datetime.now(UTC),
    )
    public = LiveEventAuthorizationPolicy().public_envelope(event, frozenset({"read"}))
    assert public.sequence == event.sequence
    assert public.kind == "loop.event.redacted"
    assert public.entity_type == "redacted_event"
    assert public.entity_id == event.event_id
    assert public.correlation_id is None
    assert public.causation_id is None
    assert public.payload == {"status": "redacted"}
    assert "private" not in str(public.model_dump())
