"""StreamBridge 慢订阅者、环形缓冲缺口及发布积压回归。"""

import asyncio

from focus.runtime.stream_bridge.memory import MemoryStreamBridge
from focus.runtime.stream_bridge.schemas import END_SENTINEL, StreamEvent


def event(value: int) -> StreamEvent:
    return StreamEvent(id="", event="tokens", data={"text": str(value)})


def test_paused_consumer_does_not_block_fast_subscriber():
    async def scenario():
        bridge = MemoryStreamBridge(queue_maxsize=512)
        bridge.publish("r", event(1))
        await asyncio.sleep(0)
        slow = bridge.subscribe("r", heartbeat_interval=0.01)
        fast = bridge.subscribe("r", heartbeat_interval=0.01)
        try:
            assert (await anext(slow)).id == "1"
            assert (await asyncio.wait_for(anext(fast), 0.2)).id == "1"
            for value in range(2, 516):
                bridge.publish("r", event(value))
                frame = await asyncio.wait_for(anext(fast), 0.2)
                assert frame.id == str(value)
                assert frame.event == "tokens"
            bridge.publish_end("r")
            assert await asyncio.wait_for(anext(fast), 0.2) is END_SENTINEL
            assert (await anext(slow)).event == "resync"
            frames = [frame async for frame in slow]
            assert frames[-2].id == "515"
            assert frames[-1] is END_SENTINEL
        finally:
            await slow.aclose()
            await fast.aclose()
    asyncio.run(scenario())


def test_default_capacity_pause_and_resume_reports_gap_and_delivers_retained_frames():
    async def scenario():
        bridge = MemoryStreamBridge(queue_maxsize=512)
        bridge.publish("r", event(1))
        await asyncio.sleep(0)
        slow = bridge.subscribe("r", heartbeat_interval=0.01)
        try:
            assert (await anext(slow)).id == "1"
            for value in range(2, 516):
                bridge.publish("r", event(value))
            await asyncio.sleep(0)
            gap = await asyncio.wait_for(anext(slow), 0.2)
            assert gap.event == "resync"
            assert gap.data == {"reason": "buffer_overrun", "after_event_id": "1", "resume_after_event_id": "3"}
            bridge.publish_end("r")
            frames = [frame async for frame in slow]
            assert [frame.id for frame in frames[:-1]] == [str(i) for i in range(4, 516)]
            assert frames[-1] is END_SENTINEL
        finally:
            await slow.aclose()
    asyncio.run(scenario())


def test_burst_has_no_per_frame_background_tasks():
    async def scenario():
        bridge = MemoryStreamBridge(queue_maxsize=512)
        before = set(asyncio.all_tasks())
        for value in range(10000):
            bridge.publish("r", event(value))
        bridge.publish_end("r")
        pending = set(asyncio.all_tasks()) - before
        assert not pending, f"burst left {len(pending)} background tasks"
        assert len(bridge._streams["r"].events) == 512
    asyncio.run(scenario())


def test_reconnect_and_repeated_overruns_keep_monotonic_ids_and_terminal_order():
    async def scenario():
        bridge = MemoryStreamBridge(queue_maxsize=2)
        for value in range(1, 6):
            bridge.publish("r", event(value))
        stream = bridge.subscribe("r", last_event_id="1", heartbeat_interval=0.01)
        try:
            assert (await anext(stream)).id == "3"  # 缺口不占用下一条普通帧的 ID。
            assert (await anext(stream)).id == "4"
            for value in range(6, 10):
                bridge.publish("r", event(value))
            assert (await anext(stream)).id == "5"  # 已复制批次仍可靠输出。
            gap = await anext(stream)
            assert gap.event == "resync" and gap.id == "7"
            assert (await anext(stream)).id == "8"
            bridge.publish_end("r")
            bridge.publish("r", event(10))  # 终态后不再接收。
            assert (await anext(stream)).id == "9"
            assert await anext(stream) is END_SENTINEL
        finally:
            await stream.aclose()
    asyncio.run(scenario())


def test_heartbeat_pause_and_cancel_do_not_block_waiting_subscribers():
    async def scenario():
        from focus.runtime.stream_bridge.schemas import HEARTBEAT_SENTINEL

        bridge = MemoryStreamBridge()
        paused = bridge.subscribe("r", heartbeat_interval=0.001)
        waiting = bridge.subscribe("r", heartbeat_interval=1)
        cancelled = bridge.subscribe("r", heartbeat_interval=1)
        try:
            assert await anext(paused) is HEARTBEAT_SENTINEL
            ready = asyncio.create_task(anext(waiting))
            abandoned = asyncio.create_task(anext(cancelled))
            await asyncio.sleep(0)
            abandoned.cancel()
            await asyncio.gather(abandoned, return_exceptions=True)
            bridge.publish("r", event(1))
            assert (await asyncio.wait_for(ready, 0.2)).id == "1"
            assert (await anext(paused)).id == "1"
        finally:
            await paused.aclose()
            await waiting.aclose()
            await cancelled.aclose()
    asyncio.run(scenario())


def test_critical_frames_and_end_are_forwarded_without_coalescing():
    async def scenario():
        bridge = MemoryStreamBridge(queue_maxsize=3)
        for kind in ("tokens", "events", "interrupt", "error"):
            bridge.publish("r", StreamEvent(id="", event=kind, data={"kind": kind}))
        bridge.publish_end("r")
        frames = [frame async for frame in bridge.subscribe("r")]
        assert [frame.event for frame in frames] == ["resync", "events", "interrupt", "error", "__end__"]
        assert frames[-1] is END_SENTINEL
    asyncio.run(scenario())


def test_sse_consumer_forwards_gap_and_terminal_status_after_pause():
    from types import SimpleNamespace

    from backend.app.gateway.routers.thread_runs import sse_consumer
    from focus.runtime.runs.manager import RunManager
    from focus.runtime.runs.schemas import RunStatus

    async def scenario():
        bridge = MemoryStreamBridge(queue_maxsize=2)
        manager = RunManager()
        record = manager.create("thread", run_id="r")
        async def connected():
            return False
        request = SimpleNamespace(headers={}, is_disconnected=connected)
        consumer = sse_consumer(bridge, record, request, manager)
        try:
            bridge.publish("r", event(1))
            assert "id: 1" in await anext(consumer)
            for value in range(2, 6):
                bridge.publish("r", event(value))
            record.status = RunStatus.interrupted
            bridge.publish_end("r")
            frames = [frame async for frame in consumer]
            assert "event: resync" in frames[0] and "id: 3" in frames[0]
            assert "id: 4" in frames[1] and "id: 5" in frames[2]
            assert "event: end" in frames[3] and '"status": "interrupted"' in frames[3]
        finally:
            await consumer.aclose()
    asyncio.run(scenario())


def test_nonpositive_capacity_is_rejected():
    import pytest
    from pydantic import ValidationError

    from focus.config.stream_bridge_config import StreamBridgeConfig

    for size in (0, -1):
        with pytest.raises(ValueError):
            MemoryStreamBridge(queue_maxsize=size)
        with pytest.raises(ValidationError):
            StreamBridgeConfig(queue_maxsize=size)
