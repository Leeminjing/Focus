"""桌面领域共享的值对象、约束或纯函数。"""

from __future__ import annotations

import json
from typing import Any

from focus.agents.image_inputs import RunImageInputs
from focus.messages import (
    estimate_images_tokens,
    estimate_messages_tokens,
    estimate_model_request_tokens,
    estimate_raw_tokens,
    strip_image_payloads,
)


def estimate_tokens(
    system_prompt: str,
    messages: list[dict[str, Any]],
    final_message: str,
    run_images: RunImageInputs | None = None,
) -> int:
    image_tokens = estimate_images_tokens(messages)
    counted = strip_image_payloads(messages) if image_tokens else messages
    raw = (
        system_prompt
        + final_message
        + json.dumps(counted, ensure_ascii=False, separators=(",", ":"))
    )
    state_total = estimate_raw_tokens(raw, len(messages)) + image_tokens
    if run_images is None:
        return state_total
    request_extra = estimate_model_request_tokens(
        messages, run_images.attached
    ) - estimate_messages_tokens(messages)
    return state_total + request_extra
