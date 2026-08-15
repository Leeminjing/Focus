"""
本文件对外提供 ContextProjection 与 compile_context_messages。

输入为用户自由编写的 messages 字典列表；输出为保留原定义的合法执行投影、稳定哈希、
无损修补清单和需要用户批准的局部降级候选。具体工作流为先生成不含正文的协议签名并
用正则标记可疑 Tool 序列，再以结构规则补充合成消息，最后复用统一消息校验。任何需要
修改原消息的处理只形成候选并返回 approval_required，不会被静默采用。

示例：`result = compile_context_messages([{"role": "tool", ...}])`。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Literal

from focus.runtime.runs.events import validate_messages


ProjectionStatus = Literal["valid", "repaired", "approval_required"]
_ROLE_TOKEN = {"human": "H", "user": "H", "ai": "A", "assistant": "A", "system": "S", "tool": "T"}
_SUSPICIOUS_TOOL_SEQUENCE = re.compile(r"(?:^|[HAS])T|A(?:$|[HAS])")


@dataclass(frozen=True)
class ContextProjection:
    status: ProjectionStatus
    authored_messages: list[dict[str, Any]]
    execution_messages: list[dict[str, Any]]
    repair_manifest: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    definition_hash: str
    projection_hash: str
    protocol_signature: str

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _signature(messages: list[dict[str, Any]]) -> str:
    return "".join(_ROLE_TOKEN.get(message.get("role"), "X") for message in messages)


def _synthetic_id(definition_hash: str, kind: str, index: int, call_id: str) -> str:
    digest = hashlib.sha256(f"{definition_hash}:{kind}:{index}:{call_id}".encode()).hexdigest()[:20]
    return f"focus-synthetic-{digest}"


def _degraded_message(message: dict[str, Any], index: int) -> dict[str, Any]:
    role = message.get("role", "unknown")
    content = json.dumps(message, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    return {
        "role": "human",
        "content": f'<focus-degraded-message index="{index + 1}" role="{role}">\n{content}\n</focus-degraded-message>',
        "id": f"focus-degraded-{_canonical_hash([index, message])[:20]}",
    }


def _message_issue(message: dict[str, Any], index: int, seen_call_ids: set[str]) -> str | None:
    role = message.get("role")
    if role not in _ROLE_TOKEN:
        return "角色无效"
    if not isinstance(message.get("content", ""), (str, list)):
        return "content 必须是文本或内容块"
    tool_calls = message.get("tool_calls", [])
    if tool_calls and role not in {"ai", "assistant"}:
        return "tool_calls 只能属于 AIMessage"
    if tool_calls and not isinstance(tool_calls, list):
        return "tool_calls 必须是数组"
    for call in tool_calls if isinstance(tool_calls, list) else []:
        if not isinstance(call, dict):
            return "tool call 必须是对象"
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in seen_call_ids:
            return "tool call id 缺失或重复"
        if not isinstance(call.get("name"), str) or not call["name"]:
            return "tool call name 缺失"
        if not isinstance(call.get("args", {}), dict):
            return "tool call args 必须是对象"
    if role == "tool":
        if not isinstance(message.get("tool_call_id"), str) or not message["tool_call_id"]:
            return "ToolMessage 缺少 tool_call_id"
        if not isinstance(message.get("name"), str) or not message["name"]:
            return "ToolMessage 缺少 name"
    return None


def compile_context_messages(messages: list[dict[str, Any]]) -> ContextProjection:
    authored = deepcopy(messages)
    definition_hash = _canonical_hash(authored)
    signature = _signature(authored)
    suspicious_indexes = {
        index
        for match in _SUSPICIOUS_TOOL_SEQUENCE.finditer(signature)
        for index in range(match.start(), match.end())
        if index < len(authored)
    }
    candidate: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen_call_ids: set[str] = set()

    for index, message in enumerate(authored):
        reason = _message_issue(message, index, seen_call_ids)
        if reason:
            replacement = _degraded_message(message, index)
            issues.append(
                {
                    "index": index,
                    "reason": reason,
                    "original": deepcopy(message),
                    "proposed": replacement,
                    "diff": {"operation": "replace", "from": message, "to": replacement},
                }
            )
            candidate.append(replacement)
            continue
        for call in message.get("tool_calls", []):
            seen_call_ids.add(call["id"])
        candidate.append(deepcopy(message))

    execution: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    resolved: set[str] = set()

    def finish_pending(before_index: int) -> None:
        for call_id, call in list(pending.items()):
            if call_id in resolved:
                continue
            synthetic = {
                "role": "tool",
                "content": "[Focus placeholder: tool result omitted]",
                "id": _synthetic_id(definition_hash, "tool-result", before_index, call_id),
                "tool_call_id": call_id,
                "name": call["name"],
                "curation_synthetic": True,
            }
            execution.append(synthetic)
            resolved.add(call_id)
            repairs.append(
                {"kind": "missing_tool_result", "before_index": before_index, "call_id": call_id, "message": synthetic}
            )

    for index, message in enumerate(candidate):
        role = message.get("role")
        if role != "tool" and pending and set(pending) - resolved:
            finish_pending(index)
            pending = {}
            resolved = set()
        if role in {"ai", "assistant"} and message.get("tool_calls"):
            pending = {call["id"]: call for call in message["tool_calls"]}
            resolved = set()
            execution.append(message)
            continue
        if role == "tool":
            call_id = message["tool_call_id"]
            if call_id in pending and call_id not in resolved:
                execution.append(message)
                resolved.add(call_id)
                continue
            if pending and set(pending) - resolved:
                finish_pending(index)
                pending = {}
                resolved = set()
            if call_id in seen_call_ids:
                replacement = _degraded_message(message, index)
                issues.append(
                    {
                        "index": index,
                        "reason": "ToolMessage 重复使用已出现的 tool_call_id",
                        "original": deepcopy(message),
                        "proposed": replacement,
                        "diff": {"operation": "replace", "from": message, "to": replacement},
                    }
                )
                execution.append(replacement)
                continue
            synthetic = {
                "role": "ai",
                "content": "",
                "id": _synthetic_id(definition_hash, "tool-call", index, call_id),
                "tool_calls": [{"id": call_id, "name": message["name"], "args": {}}],
                "curation_synthetic": True,
            }
            execution.extend([synthetic, message])
            repairs.append(
                {"kind": "missing_tool_call", "before_index": index, "call_id": call_id, "message": synthetic}
            )
            seen_call_ids.add(call_id)
            continue
        execution.append(message)

    if pending and set(pending) - resolved:
        finish_pending(len(candidate))

    try:
        validate_messages(execution)
    except ValueError as exc:
        issues.append(
            {
                "index": None,
                "reason": str(exc),
                "original": None,
                "proposed": None,
                "diff": None,
            }
        )

    status: ProjectionStatus = "approval_required" if issues else ("repaired" if repairs else "valid")
    projection_hash = _canonical_hash(execution)
    return ContextProjection(
        status=status,
        authored_messages=authored,
        execution_messages=execution,
        repair_manifest=[*repairs, {"kind": "regex_flag", "indexes": sorted(suspicious_indexes)}] if suspicious_indexes else repairs,
        issues=issues,
        definition_hash=definition_hash,
        projection_hash=projection_hash,
        protocol_signature=signature,
    )
