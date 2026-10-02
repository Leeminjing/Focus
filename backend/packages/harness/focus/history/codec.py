"""本文件对外提供无损消息 codec、typed Item 编解码和 V1 只读适配。

输入为 BaseMessage、旧消息 dict 或 FocusItem 序列；输出为可恢复消息与版本化 typed 历史。
具体工作流为保留完整 LangChain 数据、分离原生 Provider Items、关联同一输出组并确定性恢复。
示例：restored = items_to_messages(messages_to_items([AIMessage(content="done")]))。
宿主标注的 user_authored 示例与 legacy_unknown 原样保留；只有缺少显式来源的旧模型/工具记录沿原兼容推断，手写结果不提升为真实执行证明。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from langchain_core.messages import (
    AIMessage, BaseMessage, ChatMessage, HumanMessage, SystemMessage, ToolMessage,
    messages_from_dict,
)

from focus.history.contracts import FocusItem, Origin, content_hash


_ROLES = {"human": "human", "ai": "ai", "system": "system", "tool": "tool", "chat": "user"}
_NATIVE_KINDS = {"message", "reasoning", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output", "compaction"}


def serialize_history_message(message: BaseMessage) -> dict[str, Any]:
    record = message.model_dump(mode="json")
    record["_lc"] = {"type": message.type, "data": deepcopy(record)}
    record["role"] = getattr(message, "role", _ROLES.get(message.type, message.type))
    for key in ("files", "compression", "curation_synthetic", "curation_source_message_ids", "reasoning_content"):
        if key in message.additional_kwargs:
            record[key] = deepcopy(message.additional_kwargs[key])
    return record


def deserialize_history_message(record: dict[str, Any]) -> BaseMessage:
    if "_lc" in record:
        message = messages_from_dict([deepcopy(record["_lc"])])[0]
        canonical = message.model_dump(mode="json")
        for key in canonical:
            if key in record and record[key] != canonical[key]:
                raise ValueError(f"history message {key} 与 canonical payload 不一致")
        return message
    role = record.get("role")
    kwargs = {key: deepcopy(record[key]) for key in ("id", "name", "additional_kwargs", "response_metadata") if record.get(key) is not None}
    additional = kwargs.setdefault("additional_kwargs", {})
    for key in ("files", "compression", "curation_synthetic", "curation_source_message_ids", "reasoning_content"):
        if key in record:
            additional[key] = deepcopy(record[key])
    if role in {"human", "user"}:
        return HumanMessage(content=record.get("content", ""), **kwargs)
    if role in {"ai", "assistant"}:
        for key in ("tool_calls", "invalid_tool_calls", "usage_metadata"):
            if record.get(key) is not None:
                kwargs[key] = deepcopy(record[key])
        return AIMessage(content=record.get("content", ""), **kwargs)
    if role == "system":
        return SystemMessage(content=record.get("content", ""), **kwargs)
    if role == "tool":
        return ToolMessage(content=record.get("content", ""), tool_call_id=record["tool_call_id"], status=record.get("status", "success"), **kwargs)
    if role == "developer":
        return ChatMessage(content=record.get("content", ""), role="developer", **kwargs)
    raise ValueError(f"不支持的 history role: {role}")


def deserialize_history_messages(records: Iterable[dict[str, Any]]) -> list[BaseMessage]:
    return [deserialize_history_message(record) for record in records]


def messages_to_items(messages: Iterable[BaseMessage], *, origin: Origin = "legacy_unknown") -> tuple[FocusItem, ...]:
    items: list[FocusItem] = []
    for ordinal, message in enumerate(messages):
        record = serialize_history_message(message)
        message_id = message.id or "legacy:" + content_hash([ordinal, record])
        metadata = message.additional_kwargs.get("focus_context", {})
        actual_origin = metadata.get("origin", origin)
        if isinstance(message, AIMessage) and actual_origin == "legacy_unknown" and "origin" not in metadata:
            actual_origin = "provider"
        elif isinstance(message, ToolMessage) and actual_origin != "user_authored" and metadata.get("origin") != "legacy_unknown":
            actual_origin = "tool"
        native = message.additional_kwargs.get("focus_response_items")
        if native is not None:
            items.extend(_native_items(native, message_id, record, message))
            continue
        synthetic = bool(message.additional_kwargs.get("curation_synthetic"))
        if synthetic:
            actual_origin = "runtime"
        kind = ("projection_repair" if synthetic else
                metadata.get("kind", "function_call_output" if isinstance(message, ToolMessage) else "message"))
        items.append(FocusItem(
            item_id=message_id, kind=kind, origin=actual_origin,
            scope=metadata.get("scope", "execution"), payload={"message": record},
            message_id=message_id, source_refs=tuple(metadata.get("source_refs", ())),
        ))
    return tuple(items)


def legacy_to_items(records: Iterable[dict[str, Any]], *, origin: Origin = "legacy_unknown") -> tuple[FocusItem, ...]:
    items: list[FocusItem] = []
    for ordinal, record in enumerate(deepcopy(list(records))):
        message = deserialize_history_message(record)
        if "_lc" in record or "focus_context" in message.additional_kwargs or message.additional_kwargs.get("focus_response_items"):
            if message.id is None:
                message.id = "legacy:" + content_hash([ordinal, record])
            items.extend(messages_to_items([message], origin=origin))
            continue
        identity = str(record.get("id") or "legacy:" + content_hash([ordinal, record]))
        items.append(FocusItem(item_id=identity, message_id=identity,
                               kind="function_call_output" if isinstance(message, ToolMessage) else "message",
                               origin="tool" if isinstance(message, ToolMessage) else origin,
                               payload={"message": record}))
    return tuple(items)


def history_records(items: Iterable[FocusItem]) -> tuple[dict[str, Any], ...]:
    values = tuple(items)
    groups: dict[str, list[FocusItem]] = {}
    for item in values:
        groups.setdefault(item.message_id or item.item_id, []).append(item)
    result: list[dict[str, Any]] = []
    for group in groups.values():
        if len(group) == 1 and "message" in group[0].payload:
            result.append(deepcopy(group[0].payload["message"]))
        else:
            result.append(serialize_history_message(items_to_messages(group)[0]))
    return tuple(result)


def items_to_messages(items: Iterable[FocusItem | dict[str, Any]]) -> list[BaseMessage]:
    parsed = [value if isinstance(value, FocusItem) else FocusItem.model_validate(value) for value in items]
    groups: dict[str, list[FocusItem]] = {}
    order: list[str] = []
    for item in parsed:
        group = item.message_id or item.item_id
        if group not in groups:
            groups[group] = []
            order.append(group)
        groups[group].append(item)
    result: list[BaseMessage] = []
    for group in order:
        values = groups[group]
        if len(values) == 1 and "message" in values[0].payload:
            message = deserialize_history_message(values[0].payload["message"])
            if message.id is None:
                message.id = group
            result.append(message)
        else:
            result.append(_restore_native_group(values, group))
    return result


def _native_items(native: list[dict[str, Any]], message_id: str, record: dict[str, Any], message: BaseMessage) -> list[FocusItem]:
    if not native:
        raise ValueError("原生输出组不能为空")
    bridge = deepcopy(record)
    bridge["additional_kwargs"].pop("focus_response_items", None)
    bridge["_lc"]["data"]["additional_kwargs"].pop("focus_response_items", None)
    provider = message.response_metadata.get("provider", message.response_metadata.get("focus_provider"))
    version = message.response_metadata.get("projection_version", message.response_metadata.get("focus_projection_version"))
    return [FocusItem(
        item_id=f"{message_id}:output:{index}",
        kind=raw.get("type") if raw.get("type") in _NATIVE_KINDS else "unknown",
        origin="runtime" if message.additional_kwargs.get("curation_synthetic") else "tool" if isinstance(message, ToolMessage) else "provider",
        scope="execution", payload=deepcopy(raw), message_id=message_id,
        source_refs=tuple(message.additional_kwargs.get("focus_context", {}).get("source_refs", ())),
        provider=provider, projection_version=version, bridge=bridge if index == 0 else None,
    ) for index, raw in enumerate(native)]


def _restore_native_group(items: list[FocusItem], message_id: str) -> BaseMessage:
    import json

    bridge = next((deepcopy(item.bridge) for item in items if item.bridge is not None), {})
    raw_items = [deepcopy(item.payload) for item in items]
    if "_lc" in bridge:
        bridge["additional_kwargs"]["focus_response_items"] = raw_items
        bridge["_lc"]["data"]["additional_kwargs"]["focus_response_items"] = raw_items
        message = deserialize_history_message(bridge)
        _validate_native_bridge(message, raw_items)
        return message
    if len(raw_items) == 1 and raw_items[0].get("type") in {"function_call_output", "custom_tool_call_output"}:
        raw = raw_items[0]
        return ToolMessage(
            content=raw.get("output", ""), tool_call_id=raw["call_id"], id=message_id,
            name=bridge.get("name"), status=bridge.get("status", "success"),
            additional_kwargs={**bridge.get("additional_kwargs", {}), "focus_response_items": raw_items},
            response_metadata=bridge.get("response_metadata", {}),
        )
    content: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for raw in raw_items:
        if raw.get("type") == "message":
            for part in raw.get("content", []):
                if part.get("type") == "output_text":
                    content.append({"type": "text", "text": part.get("text", ""), **({"annotations": part["annotations"]} if "annotations" in part else {})})
                elif part.get("type") == "refusal":
                    content.append(deepcopy(part))
        elif raw.get("type") == "function_call":
            calls.append({"name": raw["name"], "args": json.loads(raw["arguments"]), "id": raw["call_id"], "type": "tool_call"})
        elif raw.get("type") == "custom_tool_call":
            calls.append({"name": raw["name"], "args": {"input": raw["input"]}, "id": raw["call_id"], "type": "tool_call"})
        elif raw.get("type") == "reasoning":
            content.append(deepcopy(raw))
    additional = bridge.get("additional_kwargs", {})
    additional["focus_response_items"] = raw_items
    return AIMessage(content=content, id=message_id, tool_calls=calls, additional_kwargs=additional,
                     response_metadata=bridge.get("response_metadata", {}), usage_metadata=bridge.get("usage_metadata"),
                     invalid_tool_calls=bridge.get("invalid_tool_calls", []), name=bridge.get("name"))


def _validate_native_bridge(message, raw_items):
    import json

    if isinstance(message, ToolMessage):
        outputs = [item for item in raw_items if item.get("type") in {"function_call_output", "custom_tool_call_output"}]
        if len(outputs) != 1 or outputs[0].get("call_id") != message.tool_call_id or outputs[0].get("output") != message.content:
            raise ValueError("native output 与消息 bridge 不一致")
        return
    calls = [{"id": item["call_id"], "name": item["name"], "args": json.loads(item["arguments"]) if item["type"] == "function_call" else {"input": item["input"]}}
             for item in raw_items if item.get("type") in {"function_call", "custom_tool_call"}]
    canonical_calls = [{key: call[key] for key in ("id", "name", "args")} for call in message.tool_calls]
    if calls != canonical_calls:
        raise ValueError("native calls 与消息 bridge 不一致")
    text = "".join(block.get("text", "") for item in raw_items if item.get("type") == "message"
                   for block in item.get("content", ()) if block.get("type") == "output_text")
    visible = message.content if isinstance(message.content, str) else "".join(
        block.get("text", "") for block in message.content if isinstance(block, dict) and block.get("type") in {"text", "output_text"})
    if text != visible:
        raise ValueError("native message 与消息 bridge 不一致")
