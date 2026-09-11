"""本文件对外提供压缩 apply 载荷的确定性校验函数。

对外提供:
    validate_apply_decision — 校验并规范化 resume 载荷中的压缩 apply 决定

输入:
    decision: dict — interrupt() 返回的用户决定
    messages: list[BaseMessage] — 当前 graph state 的完整 messages
    protected_material_ids: tuple[str, ...] — 本轮必需图片的材料标识；覆盖其依赖消息的范围一律拒绝

输出:
    tuple[list[dict], str | None] — (规范化 ranges, 错误信息)；错误非 None 时调用方
    SHALL NOT 修改任何状态。每条规范化 range 为 {"source_ids": [...], "replacement": str}
    或 {"source_ids": [...], "restore": True}。

具体工作流:
    (1) 校验决定类型与 apply 语义
    (2) 逐范围校验：source_ids 非空、无重复、存在于当前 messages、跨范围不重叠
    (3) 必需图片豁免：范围内任一消息引用了本轮必需材料即整体拒绝该范围
    (4) replacement 与 restore 二选一；restore 的 source 必须全部是压缩块
    (5) 其余选择完全自由：拆散 tool-call 组的范围不拒绝，由 gate._repair_protocol 兜底修复

示例:
    ranges, error = validate_apply_decision(decision, state["messages"], ("ab12",))
"""

from typing import Any

from langchain_core.messages import BaseMessage

from focus.messages import content_text
from focus.messages.material_refs import material_ref_ids


def _source_by_id(messages: list[BaseMessage]) -> dict[str, BaseMessage]:
    return {message.id: message for message in messages if message.id}


def _compression_source(message: BaseMessage) -> list[dict] | None:
    kwargs = getattr(message, "additional_kwargs", None)
    if not isinstance(kwargs, dict):
        return None
    metadata = kwargs.get("compression")
    source = metadata.get("source") if isinstance(metadata, dict) else None
    return source if isinstance(source, list) else None


def validate_apply_decision(
    decision: Any,
    messages: list[BaseMessage],
    protected_material_ids: tuple[str, ...] = (),
) -> tuple[list[dict], str | None]:
    if not isinstance(decision, dict):
        return [], "decision 必须是对象"
    if decision.get("type") != "compression":
        return [], "type 必须是 compression"
    if decision.get("decision") != "apply":
        return [], "decision 必须是 apply"
    ranges = decision.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        return [], "ranges 必须是非空列表"
    by_id = _source_by_id(messages)
    seen: set[str] = set()
    normalized: list[dict] = []
    for index, message_range in enumerate(ranges):
        if not isinstance(message_range, dict):
            return [], f"ranges[{index}] 必须是对象"
        source_ids = message_range.get("source_ids")
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or any(not isinstance(item, str) for item in source_ids)
        ):
            return [], f"ranges[{index}].source_ids 必须是非空字符串列表"
        if len(source_ids) != len(set(source_ids)):
            return [], f"ranges[{index}].source_ids 存在重复"
        missing = [item for item in source_ids if item not in by_id]
        if missing:
            return [], f"ranges[{index}].source_ids 不存在于当前 messages: {missing[:3]}"
        overlap = seen & set(source_ids)
        if overlap:
            return [], f"ranges[{index}] 与其他范围重叠: {sorted(overlap)[:3]}"
        seen.update(source_ids)
        protected = _protected_hits(source_ids, by_id, protected_material_ids)
        if protected:
            return [], (
                f"ranges[{index}] 覆盖本轮必须查看的图片所依赖的消息: {protected[:3]}"
            )
        replacement = message_range.get("replacement")
        restore = bool(message_range.get("restore"))
        delete = bool(message_range.get("delete"))
        if delete:
            if replacement not in (None, "") or restore:
                return [], f"ranges[{index}] 的 delete 与 replacement/restore 不能同时提供"
            normalized.append({"source_ids": source_ids, "delete": True})
            continue
        if restore:
            if replacement not in (None, ""):
                return [], f"ranges[{index}] 的 restore 与 replacement 不能同时提供"
            non_blocks = [
                item for item in source_ids if _compression_source(by_id[item]) is None
            ]
            if non_blocks:
                return [], f"ranges[{index}] restore 的目标必须全部是压缩块: {non_blocks[:3]}"
            normalized.append({"source_ids": source_ids, "restore": True})
            continue
        if not isinstance(replacement, str) or not replacement.strip():
            return [], f"ranges[{index}].replacement 必须是非空字符串"
        normalized.append(
            {"source_ids": source_ids, "replacement": replacement.strip()}
        )
    return normalized, None


def _protected_hits(
    source_ids: list[str],
    by_id: dict[str, BaseMessage],
    protected_material_ids: tuple[str, ...],
) -> list[str]:
    """挑出引用了本轮必需材料的消息 id；protected 为空时恒返回空列表。"""
    if not protected_material_ids:
        return []
    protected = set(protected_material_ids)
    return [
        source_id
        for source_id in source_ids
        if material_ref_ids(content_text(by_id[source_id])) & protected
    ]
