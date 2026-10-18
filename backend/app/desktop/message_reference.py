"""Project a selected answer into the user turn and retain its navigation metadata.

Offsets refer to rendered text, excluding code toolbars, in UTF-16 code units.
The quotation is user supplied context, not an instruction or trusted source.
"""
import json


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
