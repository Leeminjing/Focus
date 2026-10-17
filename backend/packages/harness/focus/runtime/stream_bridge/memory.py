"""
本文件对外提供 MemoryStreamBridge 类，作为 StreamBridge 抽象接口的进程内内存实现。

对外提供:
    MemoryStreamBridge(StreamBridge) — 基于 asyncio.Event 的有界进程内流式桥接器

内部类:
    _RunStream — 单个 run 的流式数据和流状态容器，由同一事件循环原子访问

输入:
    MemoryStreamBridge.__init__(*, queue_maxsize: int = 512):
        queue_maxsize — 每个 run 的事件缓冲区最大容量，超出时裁剪最旧事件

输出:
    MemoryStreamBridge 实例

工作流:
    MemoryStreamBridge 维护 self._streams: dict[str, _RunStream] 和 self._counters: dict[str, int]
    分别管理每个 run 的事件缓冲区和事件 ID 计数器。
    publish 同步分配 ID → 追加事件 → 裁剪超限 → Event.set（无后台发布任务）
    publish_end 同步设置 ended → Event.set
    subscribe 复制有界批次后输出；每轮检查裁剪缺口并输出 resync，要求调用方补读权威快照
    文本、工具、审批帧均不做内容合并；越界后的可靠状态恢复由快照负责，终态独立保留
    cleanup 支持立即清理与延迟清理

示例:
    from focus.runtime.stream_bridge.memory import MemoryStreamBridge
    from focus.runtime.stream_bridge.schemas import StreamEvent, HEARTBEAT_SENTINEL, END_SENTINEL

    bridge = MemoryStreamBridge(queue_maxsize=512)
    bridge.publish("run-001", StreamEvent(id="", event="metadata", data={"hello": "world"}))
    async for event in bridge.subscribe("run-001"):
        if event is END_SENTINEL:
            break
        print(event.event, event.data)
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from focus.runtime.stream_bridge.base import StreamBridge
from focus.runtime.stream_bridge.schemas import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    StreamEvent,
)

logger = logging.getLogger(__name__)


@dataclass
class _RunStream:
    """同一事件循环拥有的 run 状态；读写状态时没有 await 或 yield。

    Event 仅唤醒等待者，不承载事件数据。订阅者每轮先复制有界批次、推进游标，
    再向外 yield，因此暂停消费不会占用发布者或其他订阅者所需的资源。
    """

    events: list[StreamEvent] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    ended: bool = False
    start_offset: int = 0  # 当前 events[0] 在整个事件流中的绝对位置


class MemoryStreamBridge(StreamBridge):
    """StreamBridge 的进程内内存实现。"""

    def __init__(self, *, queue_maxsize: int = 512) -> None:
        """初始化 MemoryStreamBridge。

        输入:
            queue_maxsize: int — 每个 run 的事件缓冲区最大容量，默认 512

        输出:
            MemoryStreamBridge 实例
        """
        if queue_maxsize < 1:
            raise ValueError("queue_maxsize must be positive")
        self._maxsize = queue_maxsize
        self._streams: dict[str, _RunStream] = {}
        self._counters: dict[str, int] = {}

    # === helpers ===

    def _get_or_create_stream(self, run_id: str) -> _RunStream:
        """获取或创建指定 run_id 的 _RunStream。

        输入:
            run_id: str — run 的唯一标识

        输出:
            _RunStream 实例
        """
        if run_id not in self._streams:
            self._streams[run_id] = _RunStream()
        return self._streams[run_id]

    # === 核心操作 ===

    def publish(self, run_id: str, event: StreamEvent) -> None:
        """生产者端：给指定的 run_id 入队一条事件。

        输入:
            run_id: str      — run 的唯一标识
            event: StreamEvent — 要入队的流式事件；若 id 为空字符串则自动分配单调递增 ID

        输出:
            None
        """
        stream = self._get_or_create_stream(run_id)

        if stream.ended:
            return

        # publish/subscribe 必须在所属 asyncio 事件循环调用。此段不让出执行权，
        # 入队在返回前完成；积压只有有界 events，不存在每帧等待锁的 Task。
        run_counter = self._counters.get(run_id, 0)
        if event.id == "":
            run_counter += 1
            self._counters[run_id] = run_counter
            event = StreamEvent(id=str(run_counter), event=event.event, data=event.data)
        stream.events.append(event)
        if len(stream.events) > self._maxsize:
            trimmed = len(stream.events) - self._maxsize
            del stream.events[:trimmed]
            stream.start_offset += trimmed
        stream.changed.set()

    def publish_end(self, run_id: str) -> None:
        """同步标记终态；订阅者读完已复制批次和缓存后收到 END_SENTINEL。"""
        stream = self._get_or_create_stream(run_id)
        stream.ended = True
        stream.changed.set()

    async def subscribe(
        self,
        run_id: str,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15,
    ) -> AsyncIterator[StreamEvent]:
        """消费者端：为指定的 run_id 产出事件。

        输入:
            run_id: str             — 订阅哪个 run
            last_event_id: str | None — 断线重连起点
            heartbeat_interval: float — 心跳间隔（秒），默认 15

        输出:
            AsyncIterator[StreamEvent] — 逐条产出事件；裁剪缺口时先产出 resync，
                                        无新事件时产出 HEARTBEAT_SENTINEL，
                                        流结束时产出 END_SENTINEL
        """
        stream = self._get_or_create_stream(run_id)

        try:
            next_offset = max(0, int(last_event_id)) if last_event_id is not None else 0
        except (ValueError, TypeError):
            next_offset = 0

        while True:
            # 所有状态读取、批次复制和游标更新在首个 yield/await 前完成。
            # 每轮（包含首次连接）检查缺口，不能静默跳过已经裁剪的帧。
            gap = None
            if next_offset < stream.start_offset:
                gap = StreamEvent(
                    id=str(stream.start_offset),
                    event="resync",
                    data={
                        "reason": "buffer_overrun",
                        "after_event_id": str(next_offset),
                        "resume_after_event_id": str(stream.start_offset),
                    },
                )
                next_offset = stream.start_offset
            local_index = next_offset - stream.start_offset
            batch = stream.events[local_index:] if local_index < len(stream.events) else []
            next_offset += len(batch)
            ended = stream.ended

            if gap is not None:
                yield gap
            for event in batch:
                yield event
            if ended:
                yield END_SENTINEL
                return
            if batch or gap is not None:
                continue

            # 检查为空到 clear 之间没有 await；之后发布的 set 不会丢唤醒。
            stream.changed.clear()
            try:
                await asyncio.wait_for(stream.changed.wait(), timeout=heartbeat_interval)
            except TimeoutError:
                yield HEARTBEAT_SENTINEL

    def cleanup(self, run_id: str, delay: float | None = None) -> None:
        """释放与指定 run_id 关联的资源。

        输入:
            run_id: str          — run 的唯一标识
            delay: float | None  — 延迟秒数，None 立即清理，有值则延迟后清理

        输出:
            None
        """
        if delay is None:
            self._streams.pop(run_id, None)
            self._counters.pop(run_id, None)
            logger.info("run '%s' 流资源已清理", run_id)
            return

        async def _delayed_cleanup() -> None:
            await asyncio.sleep(delay)
            self._streams.pop(run_id, None)
            self._counters.pop(run_id, None)
            logger.info("run '%s' 流资源已延迟清理 (delay=%.1fs)", run_id, delay)

        try:
            asyncio.get_running_loop()
            asyncio.create_task(_delayed_cleanup())
        except RuntimeError:
            self._streams.pop(run_id, None)
            self._counters.pop(run_id, None)
