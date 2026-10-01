"""本文件对外提供 tool_repair_message 与 repair_tool_messages。

输入为宿主确认的缺失调用身份、原因及来源，或压缩后的兼容消息；输出为有稳定身份的非成功协议占位。
具体工作流为以 call ID 闭合缺失结果，将孤立输出降为可审计的 repair 文本；全部合成内容具有 runtime 来源、
error 状态和 exclude 资格，不声称工具执行成功。空删除标记延至交换结束，保留尚存并行结果的证据资格。
中断事实与压缩选择仍由各自调用边界验证。
示例：repair_tool_messages(messages, cause="compression")；tool_repair_message("c1", cause="interrupted", source_refs=refs)。
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from focus.history.contracts import content_hash


def tool_repair_message(call_id, *, cause, source_refs=(), name="tool", identity=None, content=None):
    refs = [*source_refs, {"cause": cause, "call_id": call_id}]
    return ToolMessage(
        id=identity or "repair:" + content_hash([call_id, refs]), tool_call_id=call_id, name=name, status="error",
        content=content or f"[Focus tool result unavailable: {cause}; execution success is not established.]",
        additional_kwargs={"curation_synthetic": True, "focus_context": {
            "kind": "projection_repair", "origin": "runtime", "scope": "execution", "source_refs": refs,
        }},
    )


def repair_tool_messages(messages, *, cause):
    pending, resolved, repaired, markers = {}, set(), [], []
    for message in messages:
        if pending and isinstance(message, HumanMessage) and not message.content and message.additional_kwargs.get("compression", {}).get("deleted"):
            markers.append(message)
            continue
        if isinstance(message, ToolMessage):
            call_id = message.tool_call_id
            if call_id in pending and call_id not in resolved:
                resolved.add(call_id)
                repaired.append(message)
            else:
                repaired.append(HumanMessage(id=message.id,
                    content=f'<focus-degraded-message role="tool" name="{message.name or ""}">\n{message.content}\n</focus-degraded-message>',
                    additional_kwargs={"curation_synthetic": True, "focus_context": {
                        "kind": "projection_repair", "origin": "runtime", "scope": "execution",
                        "source_refs": [{"cause": cause, "source_message_id": message.id, "call_id": call_id}],
                    }}))
            continue
        repaired.extend(_missing_outputs(pending, resolved, cause))
        repaired.extend(markers)
        markers = []
        pending, resolved = {}, set()
        if isinstance(message, AIMessage):
            pending = {call["id"]: (call, message.id) for call in message.tool_calls}
        repaired.append(message)
    repaired.extend(_missing_outputs(pending, resolved, cause))
    repaired.extend(markers)
    return repaired


def _missing_outputs(pending, resolved, cause):
    return [tool_repair_message(call_id, cause=cause, name=call.get("name") or "tool",
                               source_refs=({"source_message_id": source_id},))
            for call_id, (call, source_id) in pending.items() if call_id not in resolved]
