"""本文件对外提供 project_authoring_sources、source_fingerprint、freeze_authoring_sources 的纯来源投影。

输入为宿主读取的 canonical Items/旧消息、精确来源身份及记录/投影行选择；输出为稳定只读行、快照指纹或冻结 typed 条目。
工作流按逐项语义资格筛选、展开旧调用组、生成确定性行身份，确认导入后才生成目标引用；显式历史只提取白名单可读正文。
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


def source_fingerprint(records, source, *, include_historical=False):
    identity = {key: value for key, value in source.items() if key != "title"}
    return content_hash(["patrol-source-rows-v1", identity, records, include_historical])


def project_authoring_sources(records, source, *, include_historical=False):
    rows = []
    identity = {key: value for key, value in source.items() if key != "title"}
    for ordinal, record in enumerate(records):
        items = [FocusItem.model_validate(record)] if "item_id" in record else legacy_to_items([record])
        for item_index, item in enumerate(items):
            historical = semantic_policy(item) in {"exclude", "reference_only"}
            reference = _historical_payload(item) if historical else None
            allowed = not historical or include_historical and reference is not None
            seed = content_hash([identity, ordinal, item_index, item.item_id])
            values = ([{"entry_id": seed, "kind": "selected_context", "payload": reference,
                        "source_item_id": item.item_id, "source_group": item.message_id}]
                      if historical and reference is not None else expand_record(item.model_dump(mode="json"), seed))
            for value_index, value in enumerate(values):
                row_id = content_hash([seed, value_index])
                value["entry_id"] = row_id
                if historical:
                    value["reference_only"] = True
                rows.append({"source_row_id": row_id, "entry": value, "record": record, "item_id": item.item_id,
                             "message_id": item.message_id, "historical_runtime": historical, "eligible": bool(allowed),
                             "exclusion_reason": None if allowed else ("此类型仅保留在宿主历史" if reference is None else "未启用历史可读内容")})
    return rows


def freeze_authoring_sources(records, source, *, selected=None, selected_rows=None, include_historical=False):
    rows = project_authoring_sources(records, source, include_historical=include_historical)
    if selected_rows is not None:
        if not isinstance(selected_rows, list) or not selected_rows or not all(isinstance(value, str) for value in selected_rows):
            raise ValueError("请选择有效来源行")
        allowed = {row["source_row_id"] for row in rows if row["eligible"]}
        if not set(selected_rows).issubset(allowed):
            raise ValueError("来源行不存在或不可导入")
    entries, sources = [], {}
    for row in rows:
        record = row["record"]
        if not row["eligible"] or selected_rows is not None and row["source_row_id"] not in selected_rows:
            continue
        if selected is not None and (record.get("item_id") or record.get("id")) not in selected and record.get("message_id") not in selected:
            continue
        value = deepcopy(row["entry"])
        value["entry_id"] = uuid.uuid4().hex
        entry = AuthoringEntry.model_validate(value)
        entry.source_label = source.get("title") or source.get("path") or (f"Context · R{source.get('generation', '')}" if source.get("revision_id") else "历史来源")
        ref = uuid.uuid4().hex
        entry.source_ref, entry.source_hash = ref, content_hash(record)
        sources[ref] = {"record": deepcopy(record), "source": {**source, "message_id": row["message_id"] or record.get("id"),
            "item_id": row["item_id"], "source_group": value.get("source_group"), "source_row_id": row["source_row_id"], "content_hash": content_hash(record)},
            "entry_hash": content_hash([entry.kind, entry.payload]), "historical_runtime": row["historical_runtime"]}
        entries.append(entry)
    return entries, sources
