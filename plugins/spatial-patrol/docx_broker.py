"""In-process save-command broker for the manual DOCX editor plugin."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BridgeContext:
    session_id: str
    document_id: str
    plugin_url: str
    browser_bridge_origin: str
    document_server_origin: str
    document_server_internal_origins: frozenset[str] = field(
        default_factory=lambda: frozenset({"http://documentserver:80"})
    )


class BrokerError(RuntimeError):
    pass


class DocxCommandBroker:
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._results: dict[str, asyncio.Future] = {}
        self._contexts: dict[str, BridgeContext] = {}

    def register(self, context: BridgeContext) -> None:
        self._contexts[context.session_id] = context
        self._queues.setdefault(context.session_id, asyncio.Queue())

    def context(self, session_id: str) -> BridgeContext:
        try:
            return self._contexts[session_id]
        except KeyError as exc:
            raise BrokerError("DOCX bridge session is not registered") from exc

    async def issue(
        self,
        session_id: str,
        *,
        action: str,
        expected_version: int | None = None,
        timeout: float = 45,
    ) -> dict[str, Any]:
        self.context(session_id)
        if action != "force_save":
            raise BrokerError("Unsupported editor command")
        command_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._results[command_id] = future
        await self._queues[session_id].put({
            "command_id": command_id,
            "action": action,
            "expected_version": expected_version,
        })
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BrokerError("DOCX 编辑器未在时限内返回命令结果") from exc
        finally:
            self._results.pop(command_id, None)

    async def next_command(self, session_id: str, timeout: float = 25) -> dict | None:
        queue = self._queues.setdefault(session_id, asyncio.Queue())
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def complete(self, command_id: str, result: dict[str, Any]) -> None:
        future = self._results.get(command_id)
        if not future or future.done():
            raise BrokerError("命令不存在或已完成")
        if not isinstance(result.get("changed"), bool):
            raise BrokerError("命令结果缺少 changed 布尔证据")
        if not isinstance(result.get("document_version"), int):
            raise BrokerError("命令结果缺少 document_version")
        future.set_result(result)

    def unregister(self, session_id: str) -> None:
        self._contexts.pop(session_id, None)
        self._queues.pop(session_id, None)


broker = DocxCommandBroker()
