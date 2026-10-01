"""本文件对外提供 estimate_request_budget 与 estimate_responses_budget 的完整请求窗口估算。

输入为实际行为指令、工具/schema、execution history 和已选图像；输出为与压缩门共用的启发式 token 预算。
具体工作流为计入调用参数/原生 replay、独立 instructions/tools/text.format，剥离 base64 并按像素核算图像。
示例：estimate_request_budget(messages, instructions, tools, request_images=images)；estimate_responses_budget(payload)。
"""

import json

from focus.messages.blocks import strip_image_payloads
from focus.messages.usage import estimate_images_tokens, estimate_model_request_tokens, estimate_raw_tokens


def estimate_request_budget(messages, instructions="", tool_specs=(), format_spec=None, request_images=()):
    extra = {"instructions": instructions, "tools": list(tool_specs), "format": format_spec}
    for message in strip_image_payloads(list(messages)):
        calls = getattr(message, "tool_calls", ()) if not isinstance(message, dict) else message.get("tool_calls", ())
        native = getattr(message, "additional_kwargs", {}).get("focus_response_items") if not isinstance(message, dict) else None
        if calls or native:
            extra.setdefault("protocol", []).append(native if native is not None else calls)
    raw = json.dumps(extra, ensure_ascii=False, separators=(",", ":"))
    return estimate_model_request_tokens(list(messages), request_images) + max(0, estimate_raw_tokens(raw, -2))


def estimate_responses_budget(payload):
    messages = [{"content": item.get("content", item.get("output", ""))} for item in payload.get("input", ())]
    stripped = []
    for item in payload.get("input", ()):
        value = dict(item)
        key = "output" if item.get("type") == "function_call_output" else "content"
        if key in value:
            value[key] = strip_image_payloads([{"content": value[key]}])[0]["content"]
        stripped.append(value)
    raw = json.dumps({"instructions": payload.get("instructions"), "tools": payload.get("tools"),
                      "text": payload.get("text"), "input": stripped}, ensure_ascii=False, separators=(",", ":"))
    return estimate_raw_tokens(raw, len(stripped)) + estimate_images_tokens(messages)
