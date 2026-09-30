r"""本文件对外提供 LoopRunActivityBridge。

输入为既有 StreamBridge、Loop/Context/Run 身份和 Agent values 事件；输出为原流转发及持久化的模型、工具与工作区活动事件。
具体工作流为只读取当前指令消息之后的已确认消息，按消息或 tool-call 身份去重，在缩为活动摘要前提取类型化测试结果；
Writer 在同事务保存独立领域来源并追加安全事件。失败由 Writer 重试并记录降级，close 等待提交并报告未恢复的失败。
示例：`bridge.publish(run_id, stream_event)`。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

from focus.runtime.stream_bridge.base import StreamBridge
from focus.runtime.stream_bridge.schemas import StreamEvent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.event_contract import (
    CanonicalEventDraft,
    EventVisibility,
)
from backend.app.desktop.agent_loop.run_activity_writer import LoopRunActivityWriter
from backend.app.desktop.domain_evidence.tests import TestResultParser, test_identity

logger = logging.getLogger(__name__)


class LoopRunActivityBridge(StreamBridge):
    def __init__(
        self,
        delegate: StreamBridge,
        sessions: async_sessionmaker[AsyncSession],
        *,
        loop_id: str,
        context_id: str,
        run_id: str,
        correlation_id: str | None,
        anchor_message_id: str,
    ) -> None:
        self._delegate = delegate
        self._context_id = context_id
        self._run_id = run_id
        self._correlation_id = correlation_id
        self._anchor_message_id = anchor_message_id
        self._writer = LoopRunActivityWriter(sessions, loop_id)
        self._seen: set[tuple[str, str]] = set()
        self._event_ids: dict[tuple[str, str], str] = {}
        self._failures: dict[str, Exception] = {}
        self._queue: asyncio.Queue[CanonicalEventDraft | None] = asyncio.Queue()
        self._worker = asyncio.create_task(self._persist(), name=f"loop-run-activity:{run_id}")
        self._closed = False
        self._calls: dict[str, dict] = {}

    def publish(self, run_id: str, event: StreamEvent) -> None:
        self._delegate.publish(run_id, event)
        for draft in self._drafts(event):
            self._queue.put_nowait(draft)

    def publish_end(self, run_id: str) -> None:
        self._delegate.publish_end(run_id)

    async def subscribe(
        self,
        run_id: str,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15,
    ) -> AsyncIterator[StreamEvent]:
        async for event in self._delegate.subscribe(run_id, last_event_id, heartbeat_interval):
            yield event

    def cleanup(self, run_id: str, delay: float | None = None) -> None:
        self._delegate.cleanup(run_id, delay)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.join()
        self._queue.put_nowait(None)
        await self._worker
        if self._failures:
            raise RuntimeError(f"Loop Run activity journal has {len(self._failures)} unpersisted event(s)") from next(iter(self._failures.values()))

    def _drafts(self, event: StreamEvent) -> tuple[CanonicalEventDraft, ...]:
        if event.event != "events" or not isinstance(event.data, dict):
            return ()
        state = event.data.get("data")
        messages = state.get("messages") if isinstance(state, dict) else None
        if not isinstance(messages, list):
            return ()
        anchor = next((index for index, message in enumerate(messages) if isinstance(message, dict) and message.get("id") == self._anchor_message_id), None)
        if anchor is None:
            return ()
        drafts: list[CanonicalEventDraft] = []
        for message in messages[anchor + 1:]:
            if not isinstance(message, dict):
                continue
            if message.get("role") in {"assistant", "ai"} and message.get("id"):
                draft = self._model_completed(str(message["id"]))
                if draft is not None:
                    drafts.append(draft)
            for call in message.get("tool_calls") or ():
                if isinstance(call, dict) and call.get("id"):
                    self._calls[str(call["id"])] = call
                    draft = self._tool_started(str(call["id"]), str(call.get("name") or "tool"))
                    if draft is not None:
                        drafts.append(draft)
            if message.get("role") == "tool" and message.get("tool_call_id"):
                test = self._test_completed(message)
                if test is not None:
                    drafts.append(test)
                draft = self._tool_completed(message)
                if draft is not None:
                    drafts.append(draft)
                drafts.extend(self._artifacts(message))
        return tuple(drafts)

    def _model_completed(self, message_id: str) -> CanonicalEventDraft | None:
        if not self._new("model", message_id):
            return None
        return CanonicalEventDraft(
            kind="context.model.completed",
            entity_type="model_call",
            entity_id=message_id,
            entity_revision=1,
            correlation_id=self._correlation_id,
            visibility=EventVisibility(),
            payload={"run_id": self._run_id, "context_id": self._context_id, "status": "completed", "summary": "模型已完成一次响应", "source": "run_stream"},
            idempotency_key=f"context-model:{self._run_id}:{message_id}:completed",
        )

    def _tool_started(self, call_id: str, tool_name: str) -> CanonicalEventDraft | None:
        if not self._new("started", call_id):
            return None
        return CanonicalEventDraft(
            kind="context.tool.started",
            entity_type="tool",
            entity_id=call_id,
            entity_revision=1,
            correlation_id=self._correlation_id,
            visibility=EventVisibility(),
            payload={"run_id": self._run_id, "context_id": self._context_id, "tool_name": tool_name, "status": "running", "summary": f"{tool_name} 正在执行", "source": "run_stream"},
            idempotency_key=f"context-tool:{self._run_id}:{call_id}:started",
        )

    def _tool_completed(self, message: dict) -> CanonicalEventDraft | None:
        call_id = str(message["tool_call_id"])
        if not self._new("completed", call_id):
            return None
        tool_name = str(message.get("name") or "tool")
        status = str(message.get("status") or "success")
        return CanonicalEventDraft(
            kind="context.tool.completed",
            entity_type="tool",
            entity_id=call_id,
            entity_revision=2,
            correlation_id=self._correlation_id,
            visibility=EventVisibility(),
            payload={"run_id": self._run_id, "context_id": self._context_id, "tool_name": tool_name, "status": status, "summary": f"{tool_name} {status}", "source": "run_stream"},
            idempotency_key=f"context-tool:{self._run_id}:{call_id}:completed",
        )

    def _artifacts(self, message: dict) -> tuple[CanonicalEventDraft, ...]:
        drafts: list[CanonicalEventDraft] = []
        for item in message.get("files") or ():
            artifact = str(item.get("path") or item.get("name") or item) if isinstance(item, dict) else str(item)
            entity_id = uuid.uuid5(uuid.NAMESPACE_URL, f"focus:loop-artifact:{self._run_id}:{artifact}").hex
            if not self._new("artifact", entity_id):
                continue
            drafts.append(
                CanonicalEventDraft(
                    kind="context.artifact.observed",
                    entity_type="artifact",
                    entity_id=entity_id,
                    entity_revision=1,
                    correlation_id=self._correlation_id,
                    visibility=EventVisibility(),
                    payload={"run_id": self._run_id, "context_id": self._context_id, "status": "observed", "path": artifact, "summary": "工作区文件活动已确认"},
                    idempotency_key=f"context-artifact:{self._run_id}:{entity_id}",
                )
            )
        return tuple(drafts)

    def _test_completed(self, message: dict) -> CanonicalEventDraft | None:
        call_id = str(message["tool_call_id"])
        call = self._calls.get(call_id) or {}
        args = call.get("args") or {}
        content = message.get("content")
        if not isinstance(content, str):
            return None
        parsed = TestResultParser.parse(message.get("name") or call.get("name"), content, command=str(args.get("command") or args.get("cmd") or "") if isinstance(args, dict) else None, execution_status=message.get("status"))
        if parsed is None:
            return None
        identity = test_identity(self._run_id, call_id)
        if not self._new("test", identity):
            return None
        return CanonicalEventDraft(kind="context.test.completed", entity_type="test_result", entity_id=identity, entity_revision=1, correlation_id=self._correlation_id, visibility=EventVisibility(), payload={"run_id": self._run_id, "context_id": self._context_id, **parsed, "source": "run_stream"}, idempotency_key=f"context-test:{identity}:completed")

    async def _persist(self) -> None:
        while True:
            draft = await self._queue.get()
            try:
                if draft is None:
                    return
                if draft.kind == "context.tool.completed":
                    draft = draft.model_copy(update={"causation_id": self._event_ids.get(("started", draft.entity_id))})
                event = await self._writer.write(draft)
                if event is None:
                    continue
                phase = self._phase(draft.kind)
                self._event_ids[(phase, draft.entity_id)] = event.event_id
                self._failures.pop(draft.idempotency_key, None)
            except Exception as exc:
                self._seen.discard((self._phase(draft.kind), draft.entity_id))
                self._failures[draft.idempotency_key] = exc
                logger.exception("Loop Run activity event persist failed: %s", self._run_id)
            finally:
                self._queue.task_done()

    def _new(self, phase: str, entity_id: str) -> bool:
        key = (phase, entity_id)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    @staticmethod
    def _phase(kind: str) -> str:
        return "started" if kind == "context.tool.started" else "completed" if kind == "context.tool.completed" else "model" if kind == "context.model.completed" else "test" if kind == "context.test.completed" else "artifact"
