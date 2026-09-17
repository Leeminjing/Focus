r"""本文件对外提供 CompressionManifestBuilder 的有界 manifest 与精确范围规范化。

输入为精确 revision 的序列化 messages、分页参数和候选 message ids；输出为不含正文的 manifest、
协议完整的 source ids、保护证据与所选原消息。具体工作流为先把 AI tool call 及其 ToolMessage 归入
同一 protocol group，再标记 system/latest-human/material/compression 保护锚点，最后扩展选择到完整
group 并按原顺序返回。示例：`builder.normalize(messages, ("m1",), policy)`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.app.desktop.agent_loop.compression_authority.contracts import AutonomousCompressionPolicy
from focus.messages import content_text
from focus.messages.material_refs import material_ref_ids
from focus.runtime.runs.events import deserialize_messages
from focus.messages.usage import estimate_messages_tokens


@dataclass(frozen=True, slots=True)
class CompressionManifestEntry:
    message_id: str
    role: str
    protocol_group: str
    tokens: int
    protected_reason: str | None
    compression_state: str | None


@dataclass(frozen=True, slots=True)
class NormalizedCompressionRange:
    source_ids: tuple[str, ...]
    messages: tuple[dict[str, Any], ...]
    before_tokens: int
    protection_evidence: tuple[dict[str, Any], ...]


class CompressionManifestBuilder:
    def page(
        self,
        messages: tuple[dict[str, Any], ...],
        *,
        protected_message_ids: tuple[str, ...] = (),
        offset: int = 0,
        limit: int = 80,
    ) -> dict[str, Any]:
        bounded_limit = min(max(limit, 1), 120)
        entries = self._entries(messages, protected_message_ids)
        selected = entries[offset : offset + bounded_limit]
        return {
            "total": len(entries),
            "offset": offset,
            "limit": bounded_limit,
            "items": [
                {
                    "message_id": entry.message_id,
                    "role": entry.role,
                    "protocol_group": entry.protocol_group,
                    "tokens": entry.tokens,
                    "protected_reason": entry.protected_reason,
                    "compression_state": entry.compression_state,
                }
                for entry in selected
            ],
        }

    def normalize(
        self,
        messages: tuple[dict[str, Any], ...],
        requested_ids: tuple[str, ...],
        policy: AutonomousCompressionPolicy,
        *,
        protected_message_ids: tuple[str, ...] = (),
    ) -> NormalizedCompressionRange:
        entries = self._entries(messages, protected_message_ids)
        by_id = {entry.message_id: entry for entry in entries}
        if requested_ids:
            unknown = sorted(set(requested_ids) - set(by_id))
            if unknown:
                raise ValueError(f"压缩范围含未知 message id: {unknown[:3]}")
            groups = {by_id[item].protocol_group for item in requested_ids}
        else:
            groups = self._default_groups(entries, policy)
        selected_entries = [entry for entry in entries if entry.protocol_group in groups]
        selected_ids_set = {entry.message_id for entry in selected_entries}
        evidence = tuple(
            {
                "message_id": entry.message_id,
                "reason": entry.protected_reason,
                "overlap": entry.message_id in selected_ids_set,
            }
            for entry in entries
            if entry.protected_reason
        )
        overlaps = tuple(item for item in evidence if item["overlap"])
        if overlaps:
            reasons = ", ".join(f"{item['message_id']}:{item['reason']}" for item in overlaps[:4])
            raise ValueError(f"压缩范围覆盖受保护锚点: {reasons}")
        if len(selected_entries) < 2:
            raise ValueError("至少需要两个可压缩消息")
        if len(selected_entries) > policy.max_source_messages:
            raise ValueError("候选消息数超过 delegation policy")
        selected_ids = tuple(entry.message_id for entry in selected_entries)
        selected_ids_set = set(selected_ids)
        selected = tuple(message for message in messages if str(message.get("id") or "") in selected_ids_set)
        before_tokens = estimate_messages_tokens(deserialize_messages(list(selected), validate=False))
        if before_tokens > policy.max_source_tokens:
            raise ValueError("候选 token 数超过 delegation policy")
        return NormalizedCompressionRange(selected_ids, selected, before_tokens, evidence)

    def _entries(
        self,
        messages: tuple[dict[str, Any], ...],
        protected_message_ids: tuple[str, ...] = (),
    ) -> list[CompressionManifestEntry]:
        message_ids = [str(message.get("id") or "") for message in messages if message.get("id")]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("Context messages 含重复 message id，不能安全准备压缩候选")
        latest_human_id = next(
            (str(message.get("id")) for message in reversed(messages) if message.get("role") in {"human", "user"} and message.get("id")),
            None,
        )
        call_owner: dict[str, str] = {}
        result_call_ids = {
            str(message.get("tool_call_id"))
            for message in messages
            if message.get("tool_call_id")
        }
        active_protocol_ids: set[str] = set()
        for message in messages:
            message_id = str(message.get("id") or "")
            calls = message.get("tool_calls") or ()
            for call in calls:
                if isinstance(call, dict) and call.get("id"):
                    call_owner[str(call["id"])] = message_id
                    if str(call["id"]) not in result_call_ids:
                        active_protocol_ids.add(message_id)
            if message.get("role") == "tool" and message.get("tool_call_id") not in call_owner:
                active_protocol_ids.add(message_id)
        result: list[CompressionManifestEntry] = []
        for position, message in enumerate(messages):
            message_id = str(message.get("id") or "")
            if not message_id:
                continue
            tool_call_id = str(message.get("tool_call_id") or "")
            protocol_group = call_owner.get(tool_call_id) or message_id
            reason = self._protected_reason(
                message,
                message_id,
                latest_human_id,
                frozenset(protected_message_ids),
                frozenset(active_protocol_ids),
            )
            compression = message.get("compression")
            state = None
            if isinstance(compression, dict):
                state = "deleted" if compression.get("deleted") else "compressed"
            tokens = estimate_messages_tokens(deserialize_messages([message], validate=False))
            result.append(
                CompressionManifestEntry(message_id, str(message.get("role") or "unknown"), protocol_group, tokens, reason, state)
            )
        return result

    @staticmethod
    def _protected_reason(
        message: dict[str, Any],
        message_id: str,
        latest_human_id: str | None,
        protected_message_ids: frozenset[str],
        active_protocol_ids: frozenset[str],
    ) -> str | None:
        role = message.get("role")
        if role == "system":
            return "system_safety"
        if message_id in protected_message_ids:
            return "current_direct_user_message"
        if message_id == latest_human_id:
            return "latest_direct_user_message"
        if message_id in active_protocol_ids:
            return "active_tool_protocol"
        if message.get("files"):
            return "unconsumed_material"
        deserialized = deserialize_messages([message], validate=False)
        if deserialized and material_ref_ids(content_text(deserialized[0])):
            return "unconsumed_material"
        compression = message.get("compression")
        if isinstance(compression, dict):
            return "existing_compression_block"
        return None

    @staticmethod
    def _default_groups(
        entries: list[CompressionManifestEntry],
        policy: AutonomousCompressionPolicy,
    ) -> set[str]:
        selected: set[str] = set()
        count = 0
        tokens = 0
        for entry in entries:
            if entry.protected_reason:
                continue
            if entry.protocol_group in selected:
                continue
            group = [item for item in entries if item.protocol_group == entry.protocol_group]
            group_tokens = sum(item.tokens for item in group)
            if count + len(group) > policy.max_source_messages or tokens + group_tokens > policy.max_source_tokens:
                break
            selected.add(entry.protocol_group)
            count += len(group)
            tokens += group_tokens
        return selected
