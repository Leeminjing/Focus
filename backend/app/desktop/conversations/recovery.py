"""桌面领域共享的值对象、约束或纯函数。"""

from __future__ import annotations

from typing import Any


def _checkpoint_commitment_review(checkpoint: Any, stage: int) -> dict[str, Any] | None:
    pending_writes = list(getattr(checkpoint, "pending_writes", None) or [])
    for _task_id, channel, raw_value in reversed(pending_writes):
        if channel != "__interrupt__":
            continue
        values = raw_value if isinstance(raw_value, (list, tuple)) else [raw_value]
        for item in reversed(values):
            payload = getattr(item, "value", item)
            if not isinstance(payload, dict):
                continue
            try:
                payload_stage = int(payload.get("stage") or 0)
            except (TypeError, ValueError):
                continue
            if payload.get("type") == "commitment_review" and payload_stage == stage:
                return dict(payload)
    return None
