"""本文件对外提供 upgrade_document、expand_record 的只读编辑适配。

输入为旧 v2 草稿、canonical 消息或严格 FocusItem；输出为独立 kind/payload 条目。
工作流为深复制、展开调用组、保留原文与逐项组身份，不改写来源或旧部署 hash。
示例：expand_record({'role':'ai','content':'检查','tool_calls':[{'id':'c','name':'read','args':{}}]}, 'e')。
"""
from copy import deepcopy
import json


def expand_record(record: dict, identity: str) -> list[dict]:
    if "item_id" in record and "payload" in record:
        payload = deepcopy(record["payload"])
        if "message" not in payload:
            return [{"entry_id": identity, "kind": record["kind"], "payload": payload,
                     "source_item_id": record["item_id"], "source_group": record.get("message_id")}]
        values = expand_record(payload["message"], identity)
        for value in values:
            value["source_item_id"] = record["item_id"]
            value["source_group"] = record.get("message_id") or record["item_id"]
            if value["kind"] == "message" and record["kind"] != "message":
                value["kind"] = record["kind"]
        return values
    if "kind" in record and "payload" in record:
        return [deepcopy(record)]
    raw = deepcopy(record)
    metadata = raw.get("additional_kwargs", {}).get("focus_context", {})
    role = {"human": "user", "ai": "assistant"}.get(raw.get("role", "user"), raw.get("role", "user"))
    common = {key: deepcopy(value) for key, value in raw.items() if key not in {
        "role", "content", "tool_calls", "tool_call_id", "name", "status", "entry_id", "additional_kwargs", "_lc"}}
    common.update(source_group=raw.get("source_group") or raw.get("id") or identity)
    common["legacy_record"] = raw
    if role == "tool":
        return [{**common, "entry_id": identity, "kind": "function_call_output",
                 "payload": {"call_id": raw.get("tool_call_id", ""), "output": raw.get("content", ""), "status": raw.get("status", "success")}}]
    kind = metadata.get("kind", "message")
    if role in {"developer", "system"} and kind == "message":
        kind = "authored_instruction"
    entries = [{**common, "entry_id": identity, "kind": kind,
                "payload": {"role": role, "content": raw.get("content", "")}}]
    calls = raw.get("tool_calls", [])
    if isinstance(calls, list) and all(isinstance(call, dict) and isinstance(call.get("id"), str) and isinstance(call.get("name"), str) and isinstance(call.get("args", {}), dict) for call in calls):
        for index, call in enumerate(calls):
            if not isinstance(call, dict):
                entries.append({**common, "entry_id": f"{identity}:call:{index}", "kind": "unknown", "payload": {"legacy_call": deepcopy(call)}})
                continue
            entries.append({**common, "entry_id": f"{identity}:call:{index}", "kind": "function_call",
                            "payload": {"call_id": call.get("id", ""), "name": call.get("name", ""),
                                        "arguments": json.dumps(call.get("args", {}), ensure_ascii=False)}})
    else:
        entries[0]["payload"]["legacy_tool_calls"] = deepcopy(calls)
    if raw.get("type") not in (None, "message", "human", "ai", "chat", "system", "tool") or raw.get("native_item"):
        entries[0]["kind"] = "unknown"
        entries[0]["payload"]["legacy_record"] = raw
    return entries


def upgrade_document(value: dict) -> dict:
    result = deepcopy(value)
    result["entries"] = [record.model_dump(mode="json") if hasattr(record, "model_dump") else record for record in result.get("entries", [])]
    if result.get("schema_version", 2) == 3:
        return result
    result["schema_version"] = 3
    result["entries"] = [entry for index, record in enumerate(result.get("entries", []))
                         for entry in expand_record(record, record.get("entry_id", f"legacy:{index}"))]
    return result
