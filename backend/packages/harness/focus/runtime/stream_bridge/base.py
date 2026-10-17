"""
本文件对外提供 StreamBridge 抽象基类，声明进程内事件总线的统一接口。

对外提供:
    StreamBridge(ABC) — 流式桥接抽象基类，声明 publish / publish_end / subscribe / cleanup 四方法

StreamBridge SHALL 声明四个抽象方法:

    publish(run_id, event) → None
        生产者端：给指定的 run_id 入队一条 StreamEvent

    publish_end(run_id) → None
        表明当前 run_id 不会再有事件产生

    subscribe(run_id, last_event_id=None, heartbeat_interval=15) → AsyncIterator[StreamEvent]
        消费者端：为指定的 run_id 产出事件，无新事件时产出 HEARTBEAT_SENTINEL，
        流结束时产出 END_SENTINEL

    cleanup(run_id, delay=None) → None
        释放与指定 run_id 关联的资源；若传入 delay，延迟后再清理，
        给晚到的订阅者留时间读取剩余事件

输入: 无 — 本文件为纯接口定义
输出: StreamBridge 抽象基类

示例:
    from focus.runtime.stream_bridge.base import StreamBridge

    class MyBridge(StreamBridge):
        def publish(self, run_id, event): ...
        def publish_end(self, run_id): ...
        async def subscribe(self, run_id, last_event_id=None, heartbeat_interval=15): ...
        def cleanup(self, run_id, delay=None): ...
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from focus.runtime.stream_bridge.schemas import StreamEvent


class StreamBridge(ABC):
    """进程内事件总线抽象基类，解耦 agent worker（生产者）与 SSE endpoint（消费者）。"""

    @abstractmethod
    def publish(self, run_id: str, event: StreamEvent) -> None:
        """生产者端：给指定的 run_id 入队一条事件。

        输入:
            run_id: str      — run 的唯一标识
            event: StreamEvent — 要入队的流式事件

        输出:
            None
        """
        ...

    @abstractmethod
    def publish_end(self, run_id: str) -> None:
        """表明当前 run_id 不会再有事件产生。

        输入:
            run_id: str — run 的唯一标识

        输出:
            None
        """
        ...

    @abstractmethod
    async def subscribe(
        self,
        run_id: str,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15,
    ) -> AsyncIterator[StreamEvent]:
        """消费者端：为指定的 run_id 产出事件。

        输入:
            run_id: str             — 订阅哪个 run
            last_event_id: str | None — 断线重连时带上上次收到的事件 ID，从断点续读
            heartbeat_interval: float — 无新事件时的心跳间隔（秒），默认 15

        输出:
            AsyncIterator[StreamEvent] — 逐条产出事件；无新事件时产出 HEARTBEAT_SENTINEL；
                                        缓冲缺口时产出 resync，调用方须从权威快照补读；
                                        流结束时产出 END_SENTINEL
        """
        ...

    @abstractmethod
    def cleanup(self, run_id: str, delay: float | None = None) -> None:
        """释放与指定 run_id 关联的资源。

        输入:
            run_id: str          — run 的唯一标识
            delay: float | None  — 延迟秒数，None 则立即清理

        输出:
            None
        """
        ...
