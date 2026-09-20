r"""本文件对外提供 LoopRunActivityBridge。

输入为既有 StreamBridge、Loop/Context/Run/Directive identity 和 Agent values 事件；输出为不改变原流的转发以及持久化的
Tool started/completed 与 Artifact observed 规范事件。具体工作流为只读取当前指令消息之后的序列化消息，按 tool-call identity
去重，将安全摘要串行写入 per-Loop journal，close 等待队列提交。示例：`bridge.publish(run_id, stream_event)`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import asyncio
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft, EventVisibility
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from focus.runtime.stream_bridge.base import StreamBridge
from focus.runtime.stream_bridge.schemas import StreamEvent


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
        self._sessions = sessions
        self._loop_id = loop_id
        self._context_id = context_id
        self._run_id = run_id
        self._correlation_id = correlation_id
        self._anchor_message_id = anchor_message_id
        self._journal = LoopEventJournal()
        self._seen: set[tuple[str, str]] = set()
        self._event_ids: dict[tuple[str, str], str] = {}
        self._queue: asyncio.Queue[CanonicalEventDraft | None] = asyncio.Queue()
        self._worker = asyncio.create_task(self._persist(), name=f"loop-run-activity:{run_id}")
        self._closed = False

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
            for call in message.get("tool_calls") or ():
                if isinstance(call, dict) and call.get("id"):
                    draft = self._tool_started(str(call["id"]), str(call.get("name") or "tool"))
                    if draft is not None:
                        drafts.append(draft)
            if message.get("role") == "tool" and message.get("tool_call_id"):
                draft = self._tool_completed(message)
                if draft is not None:
                    drafts.append(draft)
                drafts.extend(self._artifacts(message))
        return tuple(drafts)

    def _tool_started(self, call_id: str, tool_name: str) -> CanonicalEventDraft | None:
        if not self._new("started", call_id):
            return None
        return CanonicalEventDraft(
            kind="context.tool.started",
            entity_type="tool",
            entity_id=call_id,
            entity_revision=1,
            correlation_id=self._correlation_id,
            visibility=EventVisibility(evidence_fields=("summary",)),
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
            visibility=EventVisibility(evidence_fields=("summary",)),
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
                    visibility=EventVisibility(evidence_fields=("summary", "artifact")),
                    payload={"run_id": self._run_id, "context_id": self._context_id, "status": "observed", "summary": artifact, "artifact": artifact},
                    idempotency_key=f"context-artifact:{self._run_id}:{entity_id}",
                )
            )
        return tuple(drafts)

    async def _persist(self) -> None:
        while True:
            draft = await self._queue.get()
            try:
                if draft is None:
                    return
                if draft.kind == "context.tool.completed":
                    draft = draft.model_copy(update={"causation_id": self._event_ids.get(("started", draft.entity_id))})
                async with self._sessions.begin() as session:
                    event = await self._journal.append(session, self._loop_id, draft)
                phase = "started" if draft.kind == "context.tool.started" else "completed" if draft.kind == "context.tool.completed" else "artifact"
                self._event_ids[(phase, draft.entity_id)] = event.event_id
            except Exception:
                logger.exception("Loop Run activity event persist failed: %s", self._run_id)
            finally:
                self._queue.task_done()

    def _new(self, phase: str, entity_id: str) -> bool:
        key = (phase, entity_id)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True
