r"""本文件对外提供 LoopObservationBuilder 与 observation_hash。

输入为当前 goal/grant、bounded Portfolio frontier、稳定 Run/workspace 结果、预算、gate 和 Worker 返回；
输出为不可变 LoopObservationEnvelope 及确定性哈希。具体工作流为限制集合长度和文本大小、只保留
显式事实与展开句柄，排除私有思维链和未请求全历史。示例：`envelope = builder.build(**facts)`。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.app.desktop.agent_loop.schemas import LoopObservationEnvelope


class LoopObservationBuilder:
    def __init__(self, max_items: int = 64, max_text: int = 4000) -> None:
        self._max_items = max_items
        self._max_text = max_text

    def build(self, **facts: Any) -> LoopObservationEnvelope:
        sanitized = self._sanitize(facts)
        return LoopObservationEnvelope.model_validate(sanitized)

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            return value[: self._max_text]
        if isinstance(value, dict):
            return {
                str(key): self._sanitize(item)
                for key, item in value.items()
                if str(key) not in {"chain_of_thought", "reasoning_content", "private_reasoning", "full_history"}
            }
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item) for item in value[: self._max_items]]
        return value


def observation_hash(envelope: LoopObservationEnvelope) -> str:
    payload = json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
