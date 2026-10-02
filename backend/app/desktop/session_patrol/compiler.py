"""本文件对外提供 compile_document 与 Compilation 的纯编译端口。

输入为自由文档及目标 Provider 合同；输出为严格 FocusItems、canonical messages、角色映射和逐 entry 诊断。
工作流为验证显式变换版本、保留原定义、隔离客户来源声明、检查调用闭合，再复用唯一 codec。
历史调用只成为模型输入，不登记工具成功、不执行工具。未知结构留在文档并诊断。
原生 type 支持任意 JSON 输入分类；不支持的对象/数组值产生 entry 诊断，不影响草稿保存。
示例：result = compile_document(document, provider="openai"); result.executable 可用于准入。
"""

from copy import deepcopy
from dataclasses import dataclass, field
import json

from focus.history import FocusItem, deserialize_history_message, messages_to_items, serialize_history_message, content_hash, validate_items
from .contracts import AuthoringDocument


@dataclass
class Compilation:
    document_hash: str
    messages: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    role_mappings: list[dict] = field(default_factory=list)
    transformations: list[dict] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def executable(self) -> bool:
        return not self.diagnostics


def compile_document(document: AuthoringDocument, *, provider: str, sources: dict | None = None) -> Compilation:
    result = Compilation(document.content_hash)
    if document.raw_error:
        _diagnostic(result, "raw_parse_error", [], document.raw_error)
    entries = [entry.model_dump(mode="json") for entry in document.entries]
    if len({entry["entry_id"] for entry in entries}) != len(entries):
        _diagnostic(result, "duplicate_entry_id", [e["entry_id"] for e in entries], "条目身份重复，请复制为新身份")
    entries = _transform(entries, document, result)
    calls: dict[str, str] = {}
    pending: dict[str, str] = {}
    for entry in entries:
        identity = entry["entry_id"]
        source = (sources or {}).get(entry.get("source_ref"))
        if entry.get("source_ref") and (source is None or entry.get("source_hash") != content_hash(source["record"])):
            _diagnostic(result, "source_integrity", [identity], "来源引用无法核验；可移除引用并保留手写正文")
        if entry.get("content_error") or entry.get("fields_error"):
            _diagnostic(result, "entry_parse_error", [identity], entry.get("content_error") or entry.get("fields_error"))
        if isinstance(entry.get("content"), list) and any(isinstance(block, dict) and block.get("type") == "reasoning" for block in entry["content"]):
            _diagnostic(result, "unsupported_authored_reasoning", [identity], "手写 reasoning 不具备 continuation 证明；请保留原文或显式转成文本")
        role = {"human": "user", "ai": "assistant"}.get(entry["role"], entry["role"])
        if pending and role != "tool":
            _diagnostic(result, "missing_tool_output", list(pending.values()), "调用后必须先补齐结果，或显式转成文本示例")
            pending.clear()
        record = _record(entry, role, sources or {})
        if entry.get("native_item") is not None or entry.get("type") not in (None, "message"):
            _diagnostic(result, "unsupported_native_item", [identity], "原生载荷不能作为跨分支 continuation；可显式转成文本")
            continue
        tool_calls = record.get("tool_calls", [])
        if not isinstance(tool_calls, list) or any(not isinstance(call, dict) or not isinstance(call.get("id"), str) or not isinstance(call.get("name"), str) or not isinstance(call.get("args", {}), dict) for call in tool_calls):
            _diagnostic(result, "invalid_tool_fields", [identity], "调用需为带文本 id/name 与对象 args 的数组；原文仍可保存")
            continue
        for call in record.get("tool_calls", []):
            call_id = call.get("id")
            if not call_id or call_id in calls:
                _diagnostic(result, "duplicate_call_id", [identity, calls.get(call_id, identity)], "编辑关联调用 ID 或转成文本")
            else:
                calls[call_id] = pending[call_id] = identity
        if role == "tool":
            call_id = record.get("tool_call_id")
            if not isinstance(call_id, str):
                _diagnostic(result, "invalid_tool_fields", [identity], "工具结果需有文本 tool_call_id")
                continue
            if call_id not in pending:
                _diagnostic(result, "orphan_tool_output", [identity], "结果缺少前置调用或重复；可补调用或转成文本")
            pending.pop(call_id, None)
        try:
            message = deserialize_history_message(record)
            items = messages_to_items([message], origin="user_authored")
            result.messages.append(serialize_history_message(message))
            result.items.extend(item.model_dump(mode="json") for item in items)
            actual = "system" if provider == "deepseek" and role == "developer" else role
            result.role_mappings.append({"entry_id": identity, "requested": role, "actual": actual})
        except (ValueError, TypeError, KeyError) as exc:
            _diagnostic(result, "invalid_entry", [identity], str(exc))
    if pending:
        _diagnostic(result, "missing_tool_output", list(pending.values()), "缺少结果；手动补齐或显式生成未完成 placeholder")
    if not result.diagnostics:
        try:
            validate_items(FocusItem.model_validate(item) for item in result.items)
        except ValueError as exc:
            _diagnostic(result, "protocol_conflict", [e["entry_id"] for e in entries], str(exc))
    result.fingerprint = content_hash([result.document_hash, provider, result.items, result.transformations, "session-patrol-compiler-v1"])
    return result


def _record(entry: dict, role: str, sources: dict) -> dict:
    record = {key: deepcopy(entry[key]) for key in ("content", "name", "tool_calls", "tool_call_id", "status") if key in entry}
    record.update(role=role, id="patrol-authored:" + entry["entry_id"])
    original = sources.get(entry.get("source_ref"))
    untouched = original is not None and not entry.get("edited_from") and content_hash({key: entry.get(key) for key in ("role", "content", "tool_calls", "tool_call_id", "name", "status")}) == original.get("entry_hash")
    metadata = {"origin": "user_authored", "scope": "execution", "kind": "authored_instruction" if role in {"system", "developer"} else "message",
                "requested_role": role, "source_refs": []}
    if not original and entry.get("legacy_entry_hash") == content_hash({key: entry.get(key) for key in ("role", "content", "tool_calls", "tool_call_id", "name", "status")}):
        metadata["origin"] = "legacy_unknown"
    if original:
        metadata["source_refs"] = [{**original.get("source", {}), "relation": "reference" if untouched else "edited_from", "entry_id": entry["entry_id"]}]
        source_context = original["record"].get("additional_kwargs", {}).get("focus_context", {})
        if source_context.get("kind") == "task_contract":
            metadata["kind"] = "task_contract"
    if entry.get("reference_only") or original and original.get("historical_runtime"):
        metadata.update(kind="selected_context", authority="reference")
    elif role == "tool":
        metadata["kind"] = "function_call_output"
    record["additional_kwargs"] = {"focus_context": metadata}
    return record


def _diagnostic(result: Compilation, code: str, ids: list, message: str) -> None:
    result.diagnostics.append({"code": code, "entry_ids": ids, "message": message,
                               "options": ["edit", "as_text"]})


def _transform(entries: list[dict], document: AuthoringDocument, result: Compilation) -> list[dict]:
    for plan in document.transformations:
        if plan.document_hash != document.content_hash or not set(plan.entry_ids) <= {e["entry_id"] for e in entries}:
            _diagnostic(result, "stale_transformation", plan.entry_ids, "变换计划已过期，请重新预览")
            continue
        result.transformations.append(plan.model_dump(mode="json"))
        transformed = []
        for entry in entries:
            if entry["entry_id"] in plan.entry_ids:
                entry = deepcopy(entry)
                if plan.operation == "as_text":
                    entry = {"entry_id": entry["entry_id"], "role": "user", "content": json.dumps(entry, ensure_ascii=False), "reference_only": True}
                elif plan.operation == "rename_call":
                    old, new = plan.parameters.get("old_id"), plan.parameters.get("new_id")
                    for call in _calls(entry):
                        if call.get("id") == old:
                            call["id"] = new
                    if entry.get("tool_call_id") == old:
                        entry["tool_call_id"] = new
                elif plan.operation == "placeholder":
                    transformed.append(entry)
                    for call in _calls(entry):
                        transformed.append({"entry_id": entry["entry_id"] + ":placeholder:" + str(call.get("id")), "role": "tool", "tool_call_id": call.get("id"), "status": "error", "content": "未执行：结果未知。这是用户选择的历史 placeholder。"})
                    continue
            transformed.append(entry)
        entries = transformed
    return entries


def _calls(entry):
    calls = entry.get("tool_calls", [])
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []
