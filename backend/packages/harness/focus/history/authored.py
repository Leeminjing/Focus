"""本文件对外提供 authored_message、read_authored 的 typed 作者历史桥接。

输入为 user_authored/legacy_unknown FocusItem；输出为携带同一权威 Item 的 LangGraph 消息。
工作流只构造模型历史载体，不产生 Provider continuation、工具执行证明或运行权限。
示例：messages_to_items([authored_message(item)]) == (item,)。
"""
from copy import deepcopy
import json
from langchain_core.messages import AIMessage, ChatMessage, HumanMessage, SystemMessage, ToolMessage
from focus.history.contracts import FocusItem


def read_authored(message):
    raw = message.additional_kwargs.get("focus_authored_item")
    if raw is None:
        return None
    item = FocusItem.model_validate(raw)
    if item.origin not in {"user_authored", "legacy_unknown"} or item.scope != "execution":
        raise ValueError("作者 bridge 不能提供运行权威")
    if item.kind in {"world_state_update", "round_decision_context", "projection_repair", "reasoning", "compaction", "unknown"}:
        raise ValueError("作者 bridge 不能承载运行控制或 opaque Item")
    if message.id != (item.message_id or item.item_id):
        raise ValueError("作者 bridge identity 不一致")
    expected = authored_message(item)
    if message.type != expected.type or message.content != expected.content or getattr(message, "tool_calls", None) != getattr(expected, "tool_calls", None) or getattr(message, "tool_call_id", None) != getattr(expected, "tool_call_id", None):
        raise ValueError("作者 bridge 与 typed authority 正文不一致")
    return item


def authored_message(item: FocusItem):
    payload = item.payload
    metadata = {"origin": item.origin, "scope": item.scope, "kind": item.kind,
                "source_refs": list(item.source_refs), "requested_role": payload.get("role", "user")}
    kwargs = {"id": item.message_id or item.item_id, "additional_kwargs": {
        "focus_context": metadata, "focus_authored_item": item.model_dump(mode="json")}}
    if item.kind in {"function_call_output", "custom_tool_call_output"}:
        content = payload["output"]
        return ToolMessage(content=content, tool_call_id=payload["call_id"], status=payload.get("status", "success"), **kwargs)
    if item.kind in {"function_call", "custom_tool_call"}:
        args = json.loads(payload["arguments"]) if item.kind == "function_call" else {"input": payload["input"]}
        return AIMessage(content="", tool_calls=[{"id": payload["call_id"], "name": payload["name"], "args": args}], **kwargs)
    content = deepcopy(payload.get("content", ""))
    if item.kind == "agent_collaboration" and not content:
        content = json.dumps(payload, ensure_ascii=False)
    role = payload.get("role", "user")
    if item.kind == "selected_context":
        role = "user"
    if role == "assistant":
        return AIMessage(content=content, **kwargs)
    if role == "system":
        return SystemMessage(content=content, **kwargs)
    if role == "developer":
        return ChatMessage(role=role, content=content, **kwargs)
    return HumanMessage(content=content, **kwargs)
