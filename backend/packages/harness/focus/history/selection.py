"""本文件对外提供宿主语义资格与确定性 semantic/display 消息投影。

输入为可信 FocusItems；输出为保留原始身份和调用语境的领域消费视图。
具体工作流为排除执行控制和旧 Run 的临时选择、隔离 reasoning、关联工具证据并保留引用而不自动声明事实成立。
示例：semantic_messages(legacy_to_items(records)) 供版本化语义消费者读取。
当前 selection v2 排除 authored_instruction；用户编写的任务/模型示例仍可索引，工具示例仅具 evidence_only 资格而不声明真实执行。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from focus.history.codec import history_records
from focus.history.contracts import FocusItem, SemanticPolicy


SELECTION_VERSION = "focus-semantic-selection-v2"
_EXCLUDED = {"authored_instruction", "reasoning", "world_state_update", "round_decision_context", "projection_repair", "compaction", "unknown"}


def semantic_policy(item: FocusItem) -> SemanticPolicy:
    if item.kind in _EXCLUDED or item.origin == "runtime" or item.scope in {"runtime", "round"}:
        return "exclude"
    if item.kind == "selected_context":
        return "reference_only"
    if item.kind in {"function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}:
        return "evidence_only"
    if item.kind == "agent_collaboration":
        return "evidence_only"
    return "index"


def semantic_messages(items: Iterable[FocusItem]) -> tuple[dict[str, Any], ...]:
    items = tuple(items)
    runs = [ref["run_id"] for item in items if item.scope == "run"
            for ref in item.source_refs if ref.get("run_id")]
    active_run = runs[-1] if runs else None
    groups: dict[str, list[FocusItem]] = {}
    for item in items:
        groups.setdefault(item.message_id or item.item_id, []).append(item)
    records = {identity: history_records(group)[0] for identity, group in groups.items()}
    calls = {call["id"] for identity, group in groups.items()
             if any(semantic_policy(item) != "exclude" for item in group)
             and not records[identity].get("curation_synthetic")
             for call in records[identity].get("tool_calls", ())}
    outputs = {record.get("tool_call_id") for identity, record in records.items()
               if record.get("role") == "tool" and not record.get("curation_synthetic")
               and any(semantic_policy(item) != "exclude" for item in groups[identity])}
    closed_calls = calls & outputs
    result: list[dict[str, Any]] = []
    for ordinal, (identity, group) in enumerate(groups.items()):
        selected = [item for item in group if semantic_policy(item) != "exclude"
                    and (item.scope != "run" or any(ref.get("run_id") == active_run for ref in item.source_refs))]
        if not selected:
            continue
        record = deepcopy(records[identity])
        record.setdefault("id", identity)
        if record.get("curation_synthetic"):
            continue
        if record.get("role") == "tool" and record.get("tool_call_id") not in closed_calls:
            continue
        had_calls = bool(record.get("tool_calls"))
        if had_calls:
            record["tool_calls"] = [call for call in record["tool_calls"] if call["id"] in closed_calls]
            if not record["tool_calls"] and not record.get("content"):
                continue
        record.pop("_lc", None)
        record.pop("reasoning_content", None)
        record.pop("additional_kwargs", None)
        record.pop("response_metadata", None)
        record.pop("usage_metadata", None)
        content = record.get("content", "")
        if isinstance(content, list):
            record["content"] = [deepcopy(block) for block in content if isinstance(block, str) or block.get("type") in {"text", "output_text", "input_text"}]
        policies = {semantic_policy(item) for item in selected}
        policy = "index" if "index" in policies else "reference_only" if "reference_only" in policies else "evidence_only"
        if record.get("tool_calls") and not record.get("content"):
            policy = "evidence_only"
        record["semantic_policy"] = policy
        record["source_ordinal"] = ordinal
        record["source_item_ids"] = [item.item_id for item in selected]
        result.append(record)
    return tuple(result)


def authored_items(items: Iterable[FocusItem]) -> tuple[FocusItem, ...]:
    return tuple(item for item in items if semantic_policy(item) != "exclude" and item.scope not in {"runtime", "round", "run"})
