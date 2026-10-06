"""本文件对外提供tool_exchange_closure，统一解析与质量检查的完整工具协议边界。
输入为冻结MultiSourceEvidence与精确选中引用；输出为稳定排序、去重且保留同一Assistant全部配对结果的引用集合。
具体工作流为按每个来源隔离调用身份，从选中结果找到caller，再补齐该caller的全部Tool Result；缺caller或兄弟结果明确失败。
示例：选中同时调用read_file(a.py)、read_file(b.py)的第一个结果，输出为caller及两个结果，不纳入其他独立消息。
"""

from backend.app.desktop.context_curation import EvidenceRef, MultiSourceEvidence, SourceRevisionEvidence, evidence_ref_key


def tool_exchange_closure(evidence: MultiSourceEvidence, selected: tuple[EvidenceRef, ...]) -> tuple[EvidenceRef, ...]:
    closed = {evidence_ref_key(ref): ref for ref in selected}
    selected_keys = set(closed)
    for source in evidence.sources:
        for ref in _source_exchange_refs(source, selected_keys):
            closed[evidence_ref_key(ref)] = ref
    return tuple(closed[key] for key in sorted(closed))


def _source_exchange_refs(source: SourceRevisionEvidence, selected_keys: set[tuple[str, ...]]) -> tuple[EvidenceRef, ...]:
    callers = {str(call["id"]): message for message in source.messages for call in message.tool_calls if call.get("id")}
    results = {message.tool_call_id: message for message in source.messages if message.tool_call_id}
    relevant_callers = {}
    for message in source.messages:
        if evidence_ref_key(message.ref) not in selected_keys:
            continue
        if message.tool_call_id:
            caller = callers.get(message.tool_call_id)
            if caller is None:
                raise ValueError("选中的 Tool Result 缺少 Assistant caller")
            relevant_callers[evidence_ref_key(caller.ref)] = caller
        if message.tool_calls:
            relevant_callers[evidence_ref_key(message.ref)] = message
    refs = []
    for caller in relevant_callers.values():
        refs.append(caller.ref)
        for call in caller.tool_calls:
            result = results.get(str(call.get("id") or ""))
            if result is None:
                raise ValueError("选中的 Tool Exchange 缺少 sibling Tool Result")
            refs.append(result.ref)
    return tuple(refs)
