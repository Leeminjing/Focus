"""本文件对外提供 Revision 的 V2 载荷组装与派生执行内容选择。

输入为 authored 定义、无损执行记录及可选旧版本；输出为单一权威 HistoryPayload。
具体工作流为保存精确执行 Items、区分修复来源、继承作者定义并剥离新分支的 opaque continuation。
示例：payload = build_revision_history(authored, execution, previous=current.history_payload)。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable
from langchain_core.messages import AIMessage

from focus.history import (
    FocusItem, HistoryPayload, content_hash, deserialize_history_message, legacy_to_items, serialize_history_message,
)


def definition_records(records: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for raw in records:
        record = deepcopy(raw)
        if record.get("role") not in {"human", "user", "ai", "assistant", "system", "developer", "tool"}:
            result.append(record)
            continue
        try:
            message = deserialize_history_message(record)
        except (ValueError, KeyError, TypeError):
            if "_lc" in record:
                raise
            result.append(record)
            continue
        if isinstance(message, AIMessage) and (
            message.additional_kwargs.get("focus_response_items") is not None
            or "reasoning_content" in message.additional_kwargs
        ):
            message.additional_kwargs.pop("focus_response_items", None)
            message.additional_kwargs.pop("reasoning_content", None)
            if isinstance(message.content, list):
                message.content = [block for block in message.content if not isinstance(block, dict) or block.get("type") not in {"reasoning", "compaction"}]
            message.response_metadata = {}
            record = serialize_history_message(message)
        context = message.additional_kwargs.get("focus_context", {})
        if context.get("scope") in {"runtime", "round", "run"} or context.get("origin") == "runtime":
            continue
        result.append(record)
    return tuple(result)


def build_revision_history(
    authored: Iterable[dict[str, Any]], execution: Iterable[dict[str, Any]],
    *, previous: HistoryPayload | None = None,
) -> HistoryPayload:
    execution_items = list(legacy_to_items(execution))
    for index, item in enumerate(execution_items):
        record = item.payload.get("message", {})
        if record.get("curation_synthetic"):
            execution_items[index] = item.model_copy(update={"kind": "projection_repair", "origin": "runtime"})
    return HistoryPayload(
        authored_items=_authored_items(definition_records(authored)),
        execution_items=tuple(execution_items),
        binding_refs=previous.binding_refs if previous else (),
    )


def _authored_items(records):
    records = tuple(records)
    try:
        return legacy_to_items(records, origin="curator")
    except (ValueError, KeyError, TypeError):
        pass
    result = []
    for ordinal, record in enumerate(records):
        identity = str(record.get("id") or "authored:" + content_hash([ordinal, record]))
        try:
            message = deserialize_history_message(record)
            if "_lc" in record:
                message.id = identity
                result.extend(legacy_to_items([serialize_history_message(message)], origin="curator"))
            else:
                result.append(FocusItem(item_id=identity, message_id=identity, kind="message", origin="curator",
                                        scope="revision", payload={"message": deepcopy(record)}))
        except (ValueError, KeyError, TypeError):
            if "_lc" in record:
                raise
            result.append(FocusItem(item_id=identity, message_id=identity, kind="message", origin="curator",
                                    scope="revision", payload={"message": deepcopy(record)}))
    return tuple(result)


def continued_revision_history(
    authored: Iterable[dict[str, Any]], execution: Iterable[dict[str, Any]],
    *, previous: HistoryPayload | None = None,
) -> HistoryPayload:
    existing = tuple(authored)
    records = tuple(execution)
    known = {record.get("id") for record in existing}
    additions = tuple(record for record in records if record.get("role") in {"human", "user"}
                      and record.get("id") not in known)
    return build_revision_history([*existing, *additions], records, previous=previous)
