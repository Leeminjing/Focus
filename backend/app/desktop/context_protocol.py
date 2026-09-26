r"""本文件对外提供 ToolExchangeInspector、ExecutionViewCompiler、ProtocolRepairContext 与 compile_protocol_messages。

输入为不可变 authored/checkpoint 消息和可选的已知 Run 中断事实；输出为保留原文的 authored view、Provider 合法的 execution view、
确定性 repair manifest、结构化 issues 与 projection status。具体工作流为先检查消息局部结构，再按 tool-call group 检查闭合关系，
仅对已证明属于当前 Run 的终端未闭合调用合成非成功 ToolMessage，最后执行统一 Provider 消息校验。示例：
`projection = compile_protocol_messages(messages, ProtocolRepairContext.interrupted(run_id, call_ids=("call-1",)))`。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Literal

from focus.runtime.runs.events import validate_messages

ProtocolProjectionStatus = Literal["valid", "repaired", "approval_required"]
InterruptionStatus = Literal["interrupted", "cancelled"]
_ROLE_TOKEN = {"human": "H", "user": "H", "ai": "A", "assistant": "A", "system": "S", "tool": "T"}
_SUSPICIOUS_TOOL_SEQUENCE = re.compile(r"(?:^|[HAS])T|A(?:$|[HAS])")


@dataclass(frozen=True)
class ProtocolRepairContext:
    source_kind: Literal["definition", "run_settled"] = "definition"
    interruption_status: InterruptionStatus | None = None
    source_run_id: str | None = None
    source_revision_id: str | None = None
    reason: str | None = None
    proven_call_ids: tuple[str, ...] = ()

    @classmethod
    def interrupted(
        cls,
        source_run_id: str,
        *,
        call_ids: tuple[str, ...],
        status: InterruptionStatus = "interrupted",
        reason: str | None = None,
        source_revision_id: str | None = None,
    ) -> "ProtocolRepairContext":
        return cls(
            source_kind="run_settled",
            interruption_status=status,
            source_run_id=source_run_id,
            source_revision_id=source_revision_id,
            reason=reason,
            proven_call_ids=call_ids,
        )


@dataclass(frozen=True)
class ProtocolInspection:
    authored_messages: list[dict[str, Any]]
    candidate_messages: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    definition_hash: str
    protocol_signature: str
    suspicious_indexes: tuple[int, ...]


@dataclass(frozen=True)
class ContextProtocolProjection:
    status: ProtocolProjectionStatus
    authored_messages: list[dict[str, Any]]
    execution_messages: list[dict[str, Any]]
    repair_manifest: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    definition_hash: str
    projection_hash: str
    protocol_signature: str

    def payload(self) -> dict[str, Any]:
        return asdict(self)


class ToolExchangeInspector:
    @staticmethod
    def terminal_unresolved_calls(
        messages: list[dict[str, Any]],
    ) -> tuple[str, tuple[str, ...]] | None:
        cursor = len(messages) - 1
        while cursor >= 0 and messages[cursor].get("role") == "tool":
            cursor -= 1
        if cursor < 0:
            return None
        caller = messages[cursor]
        calls = caller.get("tool_calls") or ()
        caller_id = caller.get("id")
        if caller.get("role") not in {"ai", "assistant"} or not isinstance(caller_id, str) or not caller_id:
            return None
        if not isinstance(calls, (list, tuple)) or not calls:
            return None
        call_ids = tuple(call.get("id") for call in calls if isinstance(call, dict))
        if len(call_ids) != len(calls) or any(not isinstance(call_id, str) or not call_id for call_id in call_ids):
            return None
        if len(set(call_ids)) != len(call_ids):
            return None
        results = messages[cursor + 1 :]
        result_ids = tuple(result.get("tool_call_id") for result in results)
        if len(set(result_ids)) != len(result_ids) or any(result_id not in call_ids for result_id in result_ids):
            return None
        missing = tuple(call_id for call_id in call_ids if call_id not in result_ids)
        return (caller_id, missing) if missing else None

    def inspect(self, messages: list[dict[str, Any]]) -> ProtocolInspection:
        authored = deepcopy(messages)
        definition_hash = canonical_protocol_hash(authored)
        signature = self._signature(authored)
        suspicious = self._suspicious_indexes(signature, len(authored))
        candidate: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        seen_call_ids: set[str] = set()
        for index, message in enumerate(authored):
            reason = self._message_issue(message, seen_call_ids)
            if reason is not None:
                replacement = self._degraded_message(message, index)
                issues.append(self._replacement_issue(index, reason, message, replacement))
                candidate.append(replacement)
                continue
            for call in message.get("tool_calls", []):
                seen_call_ids.add(call["id"])
            candidate.append(deepcopy(message))
        return ProtocolInspection(
            authored_messages=authored,
            candidate_messages=candidate,
            issues=issues,
            definition_hash=definition_hash,
            protocol_signature=signature,
            suspicious_indexes=suspicious,
        )

    @staticmethod
    def _signature(messages: list[dict[str, Any]]) -> str:
        return "".join(_ROLE_TOKEN.get(message.get("role"), "X") for message in messages)

    @staticmethod
    def _suspicious_indexes(signature: str, message_count: int) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    index
                    for match in _SUSPICIOUS_TOOL_SEQUENCE.finditer(signature)
                    for index in range(match.start(), match.end())
                    if index < message_count
                }
            )
        )

    @staticmethod
    def _message_issue(message: dict[str, Any], seen_call_ids: set[str]) -> str | None:
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

    @staticmethod
    def _degraded_message(message: dict[str, Any], index: int) -> dict[str, Any]:
        role = message.get("role", "unknown")
        content = json.dumps(message, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        return {
            "role": "human",
            "content": f'<focus-degraded-message index="{index + 1}" role="{role}">\n{content}\n</focus-degraded-message>',
            "id": f"focus-degraded-{canonical_protocol_hash([index, message])[:20]}",
        }

    @staticmethod
    def _replacement_issue(
        index: int,
        reason: str,
        original: dict[str, Any],
        replacement: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "index": index,
            "reason": reason,
            "original": deepcopy(original),
            "proposed": replacement,
            "diff": {"operation": "replace", "from": original, "to": replacement},
        }


class ExecutionViewCompiler:
    VERSION = "tool-exchange-compiler-v2"

    def compile(
        self,
        inspection: ProtocolInspection,
        repair_context: ProtocolRepairContext | None = None,
    ) -> ContextProtocolProjection:
        context = repair_context or ProtocolRepairContext()
        execution: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        issues = list(inspection.issues)
        pending: dict[str, dict[str, Any]] = {}
        resolved: set[str] = set()

        for index, message in enumerate(inspection.candidate_messages):
            pending, resolved = self._advance(
                inspection.definition_hash,
                index,
                message,
                pending,
                resolved,
                context,
                execution,
                repairs,
                issues,
            )
        self._flush_pending(
            inspection.definition_hash,
            len(inspection.candidate_messages),
            pending,
            resolved,
            context,
            execution,
            repairs,
            issues,
            terminal=True,
        )
        return self._projection(inspection, execution, repairs, issues)

    def _advance(
        self,
        definition_hash: str,
        index: int,
        message: dict[str, Any],
        pending: dict[str, dict[str, Any]],
        resolved: set[str],
        context: ProtocolRepairContext,
        execution: list[dict[str, Any]],
        repairs: list[dict[str, Any]],
        issues: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        role = message.get("role")
        if role != "tool":
            pending, resolved = self._flush_pending(
                definition_hash,
                index,
                pending,
                resolved,
                context,
                execution,
                repairs,
                issues,
            )
        if role in {"ai", "assistant"} and message.get("tool_calls"):
            execution.append(message)
            return {call["id"]: call for call in message["tool_calls"]}, set()
        if role == "tool":
            return self._append_tool(
                definition_hash,
                index,
                message,
                pending,
                resolved,
                context,
                execution,
                repairs,
                issues,
            )
        execution.append(message)
        return pending, resolved

    def _append_tool(
        self,
        definition_hash: str,
        index: int,
        message: dict[str, Any],
        pending: dict[str, dict[str, Any]],
        resolved: set[str],
        context: ProtocolRepairContext,
        execution: list[dict[str, Any]],
        repairs: list[dict[str, Any]],
        issues: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        call_id = message["tool_call_id"]
        if call_id in pending and call_id not in resolved:
            execution.append(message)
            resolved.add(call_id)
            return pending, resolved
        pending, resolved = self._flush_pending(
            definition_hash,
            index,
            pending,
            resolved,
            context,
            execution,
            repairs,
            issues,
        )
        if self._already_resolved(execution, call_id):
            replacement = ToolExchangeInspector._degraded_message(message, index)
            issues.append(
                ToolExchangeInspector._replacement_issue(
                    index,
                    "ToolMessage 重复使用已出现的 tool_call_id",
                    message,
                    replacement,
                )
            )
            execution.append(replacement)
            return pending, resolved
        synthetic = self._missing_call(definition_hash, index, call_id, message["name"])
        execution.extend([synthetic, message])
        repairs.append(
            {
                "kind": "missing_tool_call",
                "before_index": index,
                "call_id": call_id,
                "message": synthetic,
                "compiler_version": self.VERSION,
                "cause": "ambiguous",
            }
        )
        issues.append(
            {
                "index": index,
                "reason": "ToolMessage 缺少可验证的 Assistant tool call",
                "original": deepcopy(message),
                "proposed": synthetic,
                "diff": {"operation": "prepend", "to": synthetic},
            }
        )
        return pending, resolved

    def _flush_pending(
        self,
        definition_hash: str,
        before_index: int,
        pending: dict[str, dict[str, Any]],
        resolved: set[str],
        context: ProtocolRepairContext,
        execution: list[dict[str, Any]],
        repairs: list[dict[str, Any]],
        issues: list[dict[str, Any]],
        terminal: bool = False,
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        if not pending:
            return pending, resolved
        if not set(pending) - resolved:
            return {}, set()
        self._close_pending(
            definition_hash,
            before_index,
            pending,
            resolved,
            context,
            execution,
            repairs,
            issues,
            terminal=terminal,
        )
        return {}, set()

    def _projection(
        self,
        inspection: ProtocolInspection,
        execution: list[dict[str, Any]],
        repairs: list[dict[str, Any]],
        issues: list[dict[str, Any]],
    ) -> ContextProtocolProjection:
        self._validate_execution(execution, issues)
        has_protocol_repairs = bool(repairs)
        if inspection.suspicious_indexes:
            repairs.append(
                {"kind": "regex_flag", "indexes": list(inspection.suspicious_indexes)}
            )
        status: ProtocolProjectionStatus = (
            "approval_required" if issues else ("repaired" if has_protocol_repairs else "valid")
        )
        return ContextProtocolProjection(
            status=status,
            authored_messages=inspection.authored_messages,
            execution_messages=execution,
            repair_manifest=repairs,
            issues=issues,
            definition_hash=inspection.definition_hash,
            projection_hash=canonical_protocol_hash(execution),
            protocol_signature=inspection.protocol_signature,
        )

    def _close_pending(
        self,
        definition_hash: str,
        before_index: int,
        pending: dict[str, dict[str, Any]],
        resolved: set[str],
        context: ProtocolRepairContext,
        execution: list[dict[str, Any]],
        repairs: list[dict[str, Any]],
        issues: list[dict[str, Any]],
        *,
        terminal: bool,
    ) -> None:
        for call_id, call in pending.items():
            if call_id in resolved:
                continue
            proven = terminal and call_id in context.proven_call_ids and context.interruption_status is not None
            synthetic = self._interrupted_result(
                definition_hash,
                before_index,
                call_id,
                call,
                context,
                proven=proven,
            )
            execution.append(synthetic)
            resolved.add(call_id)
            repairs.append(
                {
                    "kind": "missing_tool_result",
                    "before_index": before_index,
                    "call_id": call_id,
                    "message": synthetic,
                    "compiler_version": self.VERSION,
                    "source_run_id": context.source_run_id if proven else None,
                    "source_revision_id": context.source_revision_id,
                    "cause": context.interruption_status if proven else "ambiguous",
                }
            )
            if not proven:
                issues.append(
                    {
                        "kind": "unresolved_tool_call",
                        "index": before_index,
                        "before_index": before_index,
                        "call_id": call_id,
                        "reason": "Tool call 缺少结果且没有可验证的中断原因",
                        "original": deepcopy(call),
                        "proposed": synthetic,
                        "diff": {"operation": "append", "to": synthetic},
                    }
                )

    @staticmethod
    def _already_resolved(messages: list[dict[str, Any]], call_id: str) -> bool:
        return any(
            message.get("role") == "tool" and message.get("tool_call_id") == call_id
            for message in messages
        )

    @staticmethod
    def _interrupted_result(
        definition_hash: str,
        before_index: int,
        call_id: str,
        call: dict[str, Any],
        context: ProtocolRepairContext,
        *,
        proven: bool,
    ) -> dict[str, Any]:
        status = context.interruption_status if proven else "incomplete"
        reason = context.reason or "Tool result is unavailable and requires approval."
        return {
            "role": "tool",
            "content": f"[Focus tool call {status}: {reason}]",
            "id": synthetic_protocol_id(
                definition_hash,
                f"tool-result-{ExecutionViewCompiler.VERSION}-{status}-{context.source_revision_id or 'unbound'}",
                before_index,
                call_id,
            ),
            "tool_call_id": call_id,
            "name": call["name"],
            "status": "error",
            "focus_interruption_status": status,
            "curation_synthetic": True,
        }

    @staticmethod
    def _missing_call(
        definition_hash: str,
        index: int,
        call_id: str,
        name: str,
    ) -> dict[str, Any]:
        return {
            "role": "ai",
            "content": "",
            "id": synthetic_protocol_id(
                definition_hash,
                f"tool-call-{ExecutionViewCompiler.VERSION}",
                index,
                call_id,
            ),
            "tool_calls": [{"id": call_id, "name": name, "args": {}}],
            "curation_synthetic": True,
        }

    @staticmethod
    def _validate_execution(
        execution: list[dict[str, Any]],
        issues: list[dict[str, Any]],
    ) -> None:
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


def canonical_protocol_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def synthetic_protocol_id(definition_hash: str, kind: str, index: int, call_id: str) -> str:
    digest = hashlib.sha256(f"{definition_hash}:{kind}:{index}:{call_id}".encode()).hexdigest()[:20]
    return f"focus-synthetic-{digest}"


def compile_protocol_messages(
    messages: list[dict[str, Any]],
    repair_context: ProtocolRepairContext | None = None,
) -> ContextProtocolProjection:
    inspection = ToolExchangeInspector().inspect(messages)
    return ExecutionViewCompiler().compile(inspection, repair_context)
