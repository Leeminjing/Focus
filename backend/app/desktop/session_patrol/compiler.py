"""本文件对外提供 compile_document 与 Compilation 的纯 typed 编译端口。

输入为可编辑 Focus 语义文档和宿主冻结来源；输出为严格 FocusItems、兼容消息、逐项诊断及投影映射。
工作流先校验显式转换与来源，再直接构造作者 Items，验证调用关联，最后生成 LangGraph bridge。
未闭合、未知类型及未完成 JSON 可保存，但不能执行；模拟调用不登记真实工具证据或提升权限。
示例：compile_document(AuthoringDocument(), provider="openai").executable 为 True。
"""
from copy import deepcopy
from dataclasses import dataclass, field
import json

from focus.history import FocusItem, items_to_messages, serialize_history_message, content_hash, validate_items
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
    if len({e["entry_id"] for e in entries}) != len(entries):
        _diagnostic(result, "duplicate_entry_id", [e["entry_id"] for e in entries], "条目身份重复")
    entries = _transform(entries, document, result)
    calls, outputs = {}, set()
    for entry in entries:
        identity, payload, kind = entry["entry_id"], deepcopy(entry["payload"]), entry["kind"]
        source = (sources or {}).get(entry.get("source_ref"))
        if entry.get("source_ref") and (source is None or entry.get("source_hash") != content_hash(source["record"])):
            _diagnostic(result, "source_integrity", [identity], "来源引用无法核验；可以移除引用并保留手写正文")
        if entry.get("content_error") or entry.get("fields_error") or entry.get("payload_error"):
            _diagnostic(result, "entry_parse_error", [identity], entry.get("content_error") or entry.get("fields_error") or entry.get("payload_error"))
        try:
            kind = _validate_payload(kind, payload)
            _link_call(kind, payload, identity, calls, outputs, result)
            refs = _references(entry, source)
            if source and source["record"].get("additional_kwargs", {}).get("focus_context", {}).get("kind") == "task_contract":
                kind = "task_contract"
            if entry.get("reference_only") or source and source.get("historical_runtime"):
                kind = "selected_context"
                payload = {"role": "user", "content": json.dumps(payload, ensure_ascii=False) if "content" not in payload else payload["content"]}
            origin = "legacy_unknown" if entry.get("legacy_entry_hash") == content_hash([entry["kind"], entry["payload"]]) and not entry.get("edited_from") else "user_authored"
            item = FocusItem(item_id="patrol-authored:" + identity, message_id="patrol-authored:" + identity,
                             kind=kind, origin=origin, scope="execution", payload=payload, source_refs=tuple(refs))
            result.items.append(item.model_dump(mode="json"))
            requested = payload.get("role", kind)
            actual = "system" if provider == "deepseek" and requested == "developer" else requested
            result.role_mappings.append({"entry_id": identity, "item_id": item.item_id, "requested": requested, "actual": actual})
        except (ValueError, TypeError, KeyError) as exc:
            code = "unsupported_authored_reasoning" if "reasoning" in str(exc) else "unsupported_native_item" if kind == "unknown" else "invalid_tool_fields" if "call" in kind or "legacy_tool_calls" in payload else "invalid_entry"
            _diagnostic(result, code, [identity], str(exc))
    for call_id, (_, identity) in calls.items():
        if call_id not in outputs:
            _diagnostic(result, "missing_tool_output", [identity], "缺少结果；补齐、转为参考文本或显式生成未完成结果")
    if not result.diagnostics:
        try:
            parsed = [FocusItem.model_validate(item) for item in result.items]
            validate_items(parsed)
            result.messages = [serialize_history_message(message) for message in items_to_messages(parsed)]
        except (ValueError, TypeError, KeyError) as exc:
            _diagnostic(result, "protocol_conflict", [e["entry_id"] for e in entries], str(exc))
    result.fingerprint = content_hash([result.document_hash, provider, result.items, result.transformations, "session-patrol-compiler-v3"])
    return result


def _validate_payload(kind, payload):
    if "legacy_tool_calls" in payload:
        raise ValueError("legacy_tool_calls 需要带 id/name/args 的调用数组")
    if kind in {"function_call", "custom_tool_call"}:
        for key in ("call_id", "name", "arguments" if kind == "function_call" else "input"):
            if not isinstance(payload.get(key), str) or not payload[key] and key in {"call_id", "name"}:
                raise ValueError(f"{key} 必须为文本，调用身份和名称不能为空")
        if kind == "function_call" and not isinstance(json.loads(payload["arguments"]), dict):
            raise ValueError("arguments 必须是 JSON 对象文本")
    elif kind in {"function_call_output", "custom_tool_call_output"}:
        if not isinstance(payload.get("call_id"), str) or not payload["call_id"]:
            raise ValueError("call_id 必须为非空文本")
        if not isinstance(payload.get("output"), (str, list)):
            raise ValueError("output 必须是文本或内容块数组")
        if payload.get("status", "success") not in {"success", "error"}:
            raise ValueError("status 必须为 success/error")
    elif kind in {"message", "authored_instruction", "task_contract", "agent_collaboration", "selected_context"}:
        role = payload.get("role", "user")
        if role not in {"user", "assistant", "developer", "system"}:
            raise ValueError("未知消息角色可以保存；执行时需要显式映射")
        if not isinstance(payload.get("content", ""), (str, list)):
            raise ValueError("content 必须是文本或内容块数组")
        if isinstance(payload.get("content"), list) and any(isinstance(block, dict) and block.get("type") == "reasoning" for block in payload["content"]):
            raise ValueError("手写 reasoning 不具备 continuation 证明")
        if kind == "message" and role in {"developer", "system"}:
            kind = "authored_instruction"
    else:
        raise ValueError(f"未知或运行专用 kind {kind} 已保留，请显式转为参考文本")
    return kind


def _link_call(kind, payload, identity, calls, outputs, result):
    if kind in {"function_call", "custom_tool_call"}:
        call_id = payload["call_id"]
        if call_id in calls:
            _diagnostic(result, "duplicate_call_id", [identity, calls[call_id][1]], "调用 ID 重复")
        else:
            calls[call_id] = (kind, identity)
    elif kind in {"function_call_output", "custom_tool_call_output"}:
        call_id = payload["call_id"]
        expected = kind.removesuffix("_output")
        if call_id not in calls or call_id in outputs or calls[call_id][0] != expected:
            _diagnostic(result, "orphan_tool_output", [identity], "结果缺少匹配前置调用、类型不一致或结果重复")
        else:
            outputs.add(call_id)


def _references(entry, source):
    if not source:
        return []
    unchanged = source.get("entry_hash") == content_hash([entry["kind"], entry["payload"]]) and not entry.get("edited_from")
    return [{**deepcopy(source.get("source", {})), "entry_id": entry["entry_id"],
             "relation": "reference" if unchanged else "edited_from"}]


def _diagnostic(result, code, ids, message):
    result.diagnostics.append({"code": code, "entry_ids": ids, "message": message, "options": ["edit", "as_text"]})


def _transform(entries, document, result):
    for plan in document.transformations:
        if plan.document_hash != document.content_hash or not set(plan.entry_ids) <= {e["entry_id"] for e in entries}:
            _diagnostic(result, "stale_transformation", plan.entry_ids, "变换计划已过期，请重新预览")
            continue
        result.transformations.append(plan.model_dump(mode="json"))
        transformed = []
        for original in entries:
            entry = deepcopy(original)
            selected = entry["entry_id"] in plan.entry_ids or entry.get("source_group") in plan.entry_ids
            if selected and plan.operation == "as_text":
                entry = {"entry_id": entry["entry_id"], "kind": "selected_context", "payload": {"role": "user", "content": json.dumps(entry, ensure_ascii=False)}, "reference_only": True}
            elif selected and plan.operation == "rename_call" and entry["payload"].get("call_id") == plan.parameters.get("old_id"):
                entry["payload"]["call_id"] = plan.parameters.get("new_id")
            transformed.append(entry)
            if selected and plan.operation == "placeholder" and entry["kind"] in {"function_call", "custom_tool_call"}:
                transformed.append({"entry_id": entry["entry_id"] + ":placeholder", "kind": entry["kind"] + "_output",
                                    "payload": {"call_id": entry["payload"].get("call_id"), "status": "error", "output": "未执行：结果未知。这是用户选择的历史 placeholder。"}})
        entries = transformed
    return entries
