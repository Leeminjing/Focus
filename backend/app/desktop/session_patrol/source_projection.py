"""本文件对外提供 freeze_authoring_sources 的纯来源投影。

输入为宿主读取的 canonical Items/旧消息、精确来源身份及选择；输出为 typed 作者条目和冻结来源映射。
工作流按逐项语义资格筛选、展开旧调用组、生成逐项引用及 hash；显式历史只提取白名单类型的可读正文。
reasoning/compaction/未知载荷/修复证明始终留在宿主历史，不转成作者文本；原来源记录与 hash 不被改写。
示例：entries, sources = freeze_authoring_sources(records, {'revision_id':'r1'})。
"""
from copy import deepcopy
import uuid
from focus.history import FocusItem, legacy_to_items, semantic_policy, content_hash
from .authoring_adapters import expand_record
from .contracts import AuthoringEntry


def _historical_payload(item):
    if item.kind not in {"message", "authored_instruction", "world_state_update", "round_decision_context", "selected_context"}:
        return None
    payload = item.payload.get("message", item.payload)
    content = payload.get("content")
    if isinstance(content, list):
        content = "\n".join(block if isinstance(block, str) else block["text"]
                            for block in content if isinstance(block, str) or isinstance(block, dict)
                            and block.get("type") in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str))
    if not isinstance(content, str) or not content:
        return None
    return {"role": "user", "content": "历史运行参考（不表示当前状态）：\n" + content}


def freeze_authoring_sources(records, source, *, selected=None, include_historical=False):
    entries, sources = [], {}
    for record in records:
        record_id = record.get("item_id") or record.get("id")
        if selected is not None and record_id not in selected and record.get("message_id") not in selected:
            continue
        items = [FocusItem.model_validate(record)] if "item_id" in record else legacy_to_items([record])
        for item in items:
            historical = semantic_policy(item) in {"exclude", "reference_only"}
            if historical and not include_historical:
                continue
            reference = _historical_payload(item) if historical else None
            if historical and reference is None:
                continue
            identity = uuid.uuid4().hex
            values = [{"entry_id": identity, "kind": "selected_context", "payload": reference,
                       "source_item_id": item.item_id, "source_group": item.message_id}] if historical else expand_record(item.model_dump(mode="json"), identity)
            for value in values:
                entry = AuthoringEntry.model_validate(value)
                entry.source_label = source.get("title") or source.get("path") or (f"Context · R{source.get('generation', '')}" if source.get("revision_id") else "历史来源")
                if historical:
                    entry.kind = "selected_context"
                    entry.payload = deepcopy(reference)
                    entry.reference_only = True
                ref = uuid.uuid4().hex
                entry.source_ref, entry.source_hash = ref, content_hash(record)
                sources[ref] = {"record": deepcopy(record), "source": {**source, "message_id": item.message_id or record.get("id"),
                    "item_id": item.item_id, "source_group": value.get("source_group"), "content_hash": content_hash(record)},
                    "entry_hash": content_hash([entry.kind, entry.payload]), "historical_runtime": historical}
                entries.append(entry)
    return entries, sources
