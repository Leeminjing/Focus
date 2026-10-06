r"""本文件对外提供 LoopObservationBuilder 与 observation_hash。

输入为当前结构化 Mission/grant、bounded Portfolio frontier、稳定 Run/workspace 结果、预算、gate 和 Worker 返回；
输出为不可变 LoopObservationEnvelope 及确定性哈希。具体工作流为限制集合长度和文本大小、只保留
显式事实与展开句柄，排除私有思维链和未请求全历史。冻结记忆、Worker 正文、直接用户原文、来源、血缘和精确 frontier 身份绕过预览截断；模型容量由独立认知读面处理，不改耐久事实。
示例：`envelope = builder.build(**facts)`。
完成准入事实保持完整；历史 envelope 缺少此字段时哈希仍按原字段集合计算，不改写冻结身份。
具体完成资格目录也保持完整，缺省字段从历史哈希排除，与新验证请求准入分开。
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
        exact = {key: value for key, value in facts.items() if key in {"previous_task_progress", "task_delta", "committed_lineage", "decision_inputs_ref", "portfolio_frontier", "base_entity_revisions", "worker_results", "user_intents", "completion_admission", "completion_eligibility"}}
        sanitized = {**self._sanitize({key: value for key, value in facts.items() if key not in exact}), **exact}
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
    document = envelope.model_dump(mode="json")
    if document.get('completion_admission') is None:
        document.pop('completion_admission', None)
    if document.get('completion_eligibility') is None:
        document.pop('completion_eligibility', None)
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
