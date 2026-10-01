"""本文件对外提供 typed 工具交换校验与兼容消息的协议校验入口。

输入为独立调用/输出 Items 或无损消息记录；输出为完整交换的调用身份集合或明确错误。
具体工作流为按 call ID 校验唯一调用、结果关联和闭合，不要求所有未知内容符合 H/A/T 邻接。
repair_interrupted_items 只凭精确 Run 中断事实闭合原生调用，共享非成功 repair 构造，合成来源永远不成为工具执行证据。
示例：validate_items(items, require_closed=False) 校验等待工具输出的模型 checkpoint。
"""

from __future__ import annotations

from typing import Any, Iterable

from focus.history.contracts import FocusItem, content_hash


def validate_items(items: Iterable[FocusItem], *, require_closed: bool = True) -> set[str]:
    calls: set[str] = set()
    outputs: set[str] = set()
    for item in items:
        payload = item.payload
        if "message" in payload:
            message = payload["message"]
            for call in message.get("tool_calls", ()):
                _add_call(calls, call.get("id"))
            if message.get("role") == "tool":
                _add_output(calls, outputs, message.get("tool_call_id"))
        elif item.kind in {"function_call", "custom_tool_call"}:
            _add_call(calls, payload.get("call_id"))
        elif item.kind in {"function_call_output", "custom_tool_call_output"}:
            _add_output(calls, outputs, payload.get("call_id"))
    missing = calls - outputs
    if missing and require_closed:
        raise ValueError("工具调用缺少结果: " + ", ".join(sorted(missing)))
    return missing


def _add_call(calls: set[str], identity: Any) -> None:
    if not isinstance(identity, str) or not identity or identity in calls:
        raise ValueError("无效或重复 tool call id")
    calls.add(identity)


def _add_output(calls: set[str], outputs: set[str], identity: Any) -> None:
    if identity not in calls or identity in outputs:
        raise ValueError("工具输出没有唯一合法调用方")
    outputs.add(identity)


def repair_interrupted_items(items, *, source_run_id, proven_call_ids, status="interrupted"):
    from focus.history.codec import messages_to_items
    from focus.history.repair import tool_repair_message

    values = tuple(items)
    missing = validate_items(values, require_closed=False)
    if not source_run_id or status not in {"interrupted", "cancelled"} or missing != set(proven_call_ids):
        raise ValueError("typed repair 需要精确 Run 中断事实及全部缺失调用身份；不能推测执行成功")
    calls = {item.payload["call_id"]: item for item in values if item.kind in {"function_call", "custom_tool_call"}
             and "message" not in item.payload}
    if not missing.issubset(calls):
        raise ValueError("legacy message repair 由显式 V1 compiler 处理")
    repaired = list(values)
    for call_id in sorted(missing):
        source = calls[call_id]
        identity = "repair:" + content_hash([source.item_id, source_run_id, status])
        text = f"[Focus tool call {status}: result unavailable; execution outcome is not established.]"
        native = {"type": "custom_tool_call_output" if source.kind == "custom_tool_call" else "function_call_output",
                  "call_id": call_id, "output": text}
        message = tool_repair_message(call_id, cause=status, identity=identity, content=text,
            source_refs=({"source_run_id": source_run_id, "source_item_id": source.item_id},))
        message.additional_kwargs["focus_response_items"] = [native]
        repaired.extend(messages_to_items([message]))
    validate_items(repaired)
    return tuple(repaired)
