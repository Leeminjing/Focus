"""Project a selected answer into the user turn and retain its navigation metadata.

Offsets refer to rendered text, excluding code toolbars, in UTF-16 code units.
The quotation is user supplied context, not an instruction or trusted source.
"""
import json

from fastapi import HTTPException


async def validate_message_reference(desktop, task_id, reference):
    """The submission target may differ from a frozen quotation's origin."""
    if reference is None or reference.task_id == task_id:
        return
    if not await desktop.contexts.permits_message_reference(task_id, reference.model_dump()):
        raise HTTPException(422, "引用必须来自当前会话或分支的冻结来源")


def contains_message_reference(messages, reference):
    pending = list(messages)
    while pending:
        message = pending.pop()
        if (message.get("additional_kwargs") or {}).get("message_reference") == reference:
            return True
        pending.extend((message.get("compression") or {}).get("source") or [])
    return False


def project_message_reference(message: dict, reference: dict | None) -> dict:
    if not reference:
        return message
    encoded = json.dumps(reference, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    block = "<focus_message_reference>\n" + encoded + "\n</focus_message_reference>"
    content = message["content"]
    content = f"{content}\n\n{block}" if isinstance(content, str) else [*content, {"type": "text", "text": block}]
    return {**message, "content": content,
            "additional_kwargs": {**message.get("additional_kwargs", {}), "message_reference": reference}}
