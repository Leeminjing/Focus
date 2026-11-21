"""本文件对外提供精确当前状态或 owner 输入的 authored、execution、semantic 与 display 读取。

输入为唯一 Context 引用、视图与调用方事务；输出为保留消息身份的正文、分页或来源身份投影。
具体工作流为仓储精确寻址，再按 typed history 或迁移后的 checkpoint 构造消费面；semantic 排除运行时控制且不追随 latest。
示例：await ContextRevisionReader(repository, checkpointer).read(session, ref, "semantic")。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from focus.history import (
    deserialize_history_message,
    history_records,
    items_to_messages,
    legacy_to_items,
    semantic_messages,
    serialize_history_message,
    validate_items,
)
from focus.history.display import display_messages
from focus.runtime.runs.events import serialize_message
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.contexts.evolution.repository import (
    ContextRevisionIdentityMismatch,
    ContextRevisionNotFound,
    ContextRevisionRepository,
)
from backend.app.desktop.contexts.evolution.schemas import (
    ContextRevisionCheckpointView,
    ContextRevisionContract,
    ContextRevisionMessageView,
    ContextRevisionMessageViewKind,
    ContextRevisionPayloadMode,
    ContextRevisionRef,
)

ContextRevisionViewKind = Literal[
    "authored",
    "execution",
    "semantic",
    "display",
    "checkpoint",
]
ContextRevisionReadResult = ContextRevisionMessageView | ContextRevisionCheckpointView


@dataclass(frozen=True)
class ContextRevisionMessagePage:
    ref: ContextRevisionRef
    messages: tuple[dict[str, Any], ...]
    total: int
    start: int
    end: int


class ContextRevisionReader:
    def __init__(self, repository: ContextRevisionRepository, checkpointer: Any) -> None:
        self._repository = repository
        self._checkpointer = checkpointer

    async def read_current(
        self,
        session: AsyncSession,
        context_id: str,
        view: ContextRevisionViewKind,
    ) -> ContextRevisionReadResult | None:
        current = await self._repository.current(session, context_id)
        if current is None:
            return None
        return await self._read_contract(session, current, view)

    async def read(
        self,
        session: AsyncSession,
        ref: ContextRevisionRef,
        view: ContextRevisionViewKind,
    ) -> ContextRevisionReadResult:
        revision = await self._repository.get(session, ref)
        return await self._read_contract(session, revision, view)

    async def read_message_page(
        self,
        revision: ContextRevisionContract,
        *,
        before: int | None,
        limit: int,
    ) -> ContextRevisionMessagePage:
        if revision.history_payload is not None:
            view = await self._messages(revision, "display")
            total = len(view.messages)
            end = total if before is None else min(max(before, 0), total)
            start = max(0, end - limit)
            return ContextRevisionMessagePage(revision.ref, view.messages[start:end], total, start, end)
        runtime = await self._runtime_message_objects(revision.ref)
        if revision.ref.payload_mode is ContextRevisionPayloadMode.CHECKPOINT:
            total = len(runtime)
            end = total if before is None else min(max(before, 0), total)
            start = max(0, end - limit)
            messages = tuple(serialize_message(message) for message in runtime[start:end])
            return ContextRevisionMessagePage(revision.ref, messages, total, start, end)

        authored = revision.authored_messages
        initial_ids = set(revision.initial_message_ids)
        runtime_total = sum(
            1 for message in runtime if self._message_id(message) not in initial_ids
        )
        total = len(authored) + runtime_total
        end = total if before is None else min(max(before, 0), total)
        start = max(0, end - limit)
        selected: list[dict[str, Any]] = []
        authored_end = min(end, len(authored))
        if start < authored_end:
            selected.extend(deepcopy(authored[start:authored_end]))
        suffix_start = max(0, start - len(authored))
        suffix_end = max(0, end - len(authored))
        if suffix_start < suffix_end:
            suffix_index = 0
            for message in runtime:
                if self._message_id(message) in initial_ids:
                    continue
                if suffix_index >= suffix_end:
                    break
                if suffix_index >= suffix_start:
                    selected.append(serialize_message(message))
                suffix_index += 1
        return ContextRevisionMessagePage(
            revision.ref,
            tuple(selected),
            total,
            start,
            end,
        )

    async def _read_contract(
        self,
        session: AsyncSession,
        revision: ContextRevisionContract,
        view: ContextRevisionViewKind,
    ) -> ContextRevisionReadResult:
        if view in {"authored", "execution", "display", "semantic"}:
            return await self._messages(revision, view)
        if view == "checkpoint":
            checkpoint = await self._checkpoint(revision.ref)
            if checkpoint is None:
                raise ContextRevisionNotFound(
                    f"revision 尚无可读取 checkpoint: {revision.ref.revision_id}"
                )
            return checkpoint
        raise ValueError(f"未知 Context revision view: {view}")

    async def _messages(
        self,
        revision: ContextRevisionContract,
        view: ContextRevisionMessageViewKind,
    ) -> ContextRevisionMessageView:
        if revision.history_payload is not None:
            payload = revision.history_payload
            items = payload.authored_items if view == "authored" else payload.execution_items
            if view == "semantic":
                messages = semantic_messages(items)
            elif view == "display":
                objects = items_to_messages(
                    item for item in items if item.scope not in {"runtime", "round"}
                    and item.kind not in {"unknown", "compaction", "reasoning", "projection_repair"}
                )
                if revision.ref.payload_mode is ContextRevisionPayloadMode.DEFINITION:
                    messages = self._definition_items_display(revision, objects)
                else:
                    messages = tuple(serialize_message(message) for message in display_messages(objects))
            else:
                messages = history_records(items)
            return ContextRevisionMessageView(ref=revision.ref, view=view, messages=messages)
        if view == "semantic":
            items = legacy_to_items(await self._legacy_execution_records(revision))
            validate_items(items)
            return ContextRevisionMessageView(ref=revision.ref, view=view, messages=semantic_messages(items))
        runtime = await self._runtime_messages(revision.ref)
        if revision.ref.payload_mode is ContextRevisionPayloadMode.CHECKPOINT:
            messages = (
                list(deepcopy(revision.execution_messages))
                if view == "execution" and revision.execution_messages
                else runtime
            )
        elif view == "authored":
            messages = list(deepcopy(revision.authored_messages))
        elif view == "execution":
            messages = runtime or list(deepcopy(revision.execution_messages))
        else:
            messages = self._definition_display(revision, runtime)
        return ContextRevisionMessageView(ref=revision.ref, view=view, messages=tuple(messages))

    async def _checkpoint(
        self,
        ref: ContextRevisionRef,
    ) -> ContextRevisionCheckpointView | None:
        checkpoint = await self._checkpoint_tuple(ref)
        if checkpoint is None:
            return None
        values = checkpoint.checkpoint.get("channel_values", {})
        messages = tuple(serialize_message(message) for message in values.get("messages", []))
        return ContextRevisionCheckpointView(
            ref=ref,
            messages=messages,
            metadata=deepcopy(getattr(checkpoint, "metadata", {}) or {}),
        )

    async def _checkpoint_tuple(self, ref: ContextRevisionRef) -> Any | None:
        if not ref.is_runnable:
            return None
        checkpoint = await self._checkpointer.aget_tuple(ref.checkpoint_config())
        if checkpoint is None:
            raise ContextRevisionNotFound(
                f"revision checkpoint 不存在: {ref.revision_id}/{ref.checkpoint_id}"
            )
        actual_id = checkpoint.config.get("configurable", {}).get("checkpoint_id")
        if actual_id != ref.checkpoint_id:
            raise ContextRevisionIdentityMismatch(
                f"checkpoint 不属于 revision: expected={ref.checkpoint_id}, actual={actual_id}"
            )
        return checkpoint

    async def _runtime_message_objects(self, ref: ContextRevisionRef) -> tuple[Any, ...]:
        checkpoint = await self._checkpoint_tuple(ref)
        if checkpoint is None:
            return ()
        values = checkpoint.checkpoint.get("channel_values", {})
        return tuple(values.get("messages", ()))

    async def _runtime_messages(self, ref: ContextRevisionRef) -> list[dict[str, Any]]:
        messages = await self._runtime_message_objects(ref)
        return [serialize_message(message) for message in messages]

    async def _legacy_execution_records(self, revision: ContextRevisionContract):
        runtime = tuple(serialize_history_message(message) for message in await self._runtime_message_objects(revision.ref))
        stored = revision.execution_messages
        if not stored or revision.ref.payload_mode is not ContextRevisionPayloadMode.CHECKPOINT:
            return runtime or stored
        by_id = {record.get("id"): record for record in runtime if record.get("id")}
        return tuple(self._canonical_source(record, by_id.get(record.get("id"))) for record in stored)

    @staticmethod
    def _canonical_source(record, source):
        if source is None or "_lc" in record or record.get("curation_synthetic") or "focus_context" in record.get("additional_kwargs", {}):
            return deepcopy(record)
        keys = ("role", "content", "tool_calls", "tool_call_id", "name", "status")
        normalized = serialize_history_message(deserialize_history_message(record))
        if all(normalized.get(key) == source.get(key) for key in keys):
            return deepcopy(source)
        return deepcopy(record)

    @staticmethod
    def _message_id(message: Any) -> str | None:
        if isinstance(message, dict):
            value = message.get("id")
        else:
            value = getattr(message, "id", None)
        return str(value) if value is not None else None

    @staticmethod
    def _definition_items_display(revision: ContextRevisionContract, objects: list[Any]) -> tuple[dict[str, Any], ...]:
        groups = {}
        for item in revision.history_payload.authored_items:
            groups.setdefault(item.message_id or item.item_id, []).append(item)
        authored, opaque = [], {}
        for identity, group in groups.items():
            try:
                authored.extend(items_to_messages(group))
            except (ValueError, KeyError, TypeError):
                if any("message" not in item.payload or "_lc" in item.payload["message"] for item in group):
                    raise
                opaque[identity] = history_records(group)
        initial_ids = set(revision.initial_message_ids) | groups.keys()
        suffix = [message for message in objects if message.id not in initial_ids]
        visible = [serialize_message(message) for message in display_messages([*authored, *suffix])]
        by_id = {message.get("id"): message for message in visible}
        prefix = [record for identity in groups for record in opaque.get(
            identity, (by_id[identity],) if identity in by_id else ()
        )]
        return (*prefix, *(message for message in visible if message.get("id") not in groups))

    @staticmethod
    def _definition_display(
        revision: ContextRevisionContract,
        runtime: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        initial_ids = set(revision.initial_message_ids)
        suffix = [message for message in runtime if message.get("id") not in initial_ids]
        return [*deepcopy(revision.authored_messages), *suffix]
