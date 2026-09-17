r"""本文件对外提供自主压缩候选哈希与稳定 checkpoint 内容证据验证。

输入为候选选择的序列化来源消息、候选 ranges 和压缩后 revision 的 execution messages；输出为携带
稳定 reason code、实际来源/摘要 token 统计的不可变证据。具体工作流为准备候选时把每个 range 的
来源与 replacement 哈希固化，恢复对账时定位压缩块并同时核对 source ids、source hash 与 replacement
hash，任何缺失或不一致都不得声明 applied。示例：`result = verifier.verify(messages, ranges)`。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from focus.messages.usage import estimate_messages_tokens
from focus.runtime.runs.events import deserialize_messages


@dataclass(frozen=True, slots=True)
class CompressionCheckpointEvidence:
    matched: bool
    reason: str
    actual_before_tokens: int = 0
    actual_after_tokens: int = 0


class CompressionEvidenceVerifier:
    @classmethod
    def bind_ranges(
        cls,
        messages: tuple[dict[str, Any], ...],
        ranges: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_id = {str(message.get("id") or ""): message for message in messages}
        bound: list[dict[str, Any]] = []
        for item in ranges:
            source = [by_id[source_id] for source_id in item["source_ids"]]
            replacement = str(item["replacement"])
            bound.append(
                {
                    **item,
                    "source_hash": cls.message_hash(source),
                    "replacement_hash": cls.text_hash(replacement),
                }
            )
        return bound

    @classmethod
    def verify(
        cls,
        messages: tuple[dict[str, Any], ...],
        ranges: list[dict[str, Any]],
    ) -> CompressionCheckpointEvidence:
        blocks = cls._compression_blocks(messages)
        before_tokens = 0
        after_tokens = 0
        matched_ids: set[str] = set()
        for item in ranges:
            source_hash = item.get("source_hash")
            replacement_hash = item.get("replacement_hash")
            if not isinstance(source_hash, str) or not isinstance(replacement_hash, str):
                return CompressionCheckpointEvidence(False, "candidate_evidence_missing")
            source_ids = tuple(item.get("source_ids") or ())
            block = next((candidate for candidate in blocks if candidate[0] == source_ids), None)
            if block is None:
                return CompressionCheckpointEvidence(False, "compression_block_missing")
            _, message, source = block
            if cls.message_hash(source) != source_hash:
                return CompressionCheckpointEvidence(False, "compressed_source_hash_mismatch")
            if cls.text_hash(cls._text(message.get("content"))) != replacement_hash:
                return CompressionCheckpointEvidence(False, "compression_replacement_hash_mismatch")
            if any(source_id in matched_ids for source_id in source_ids):
                return CompressionCheckpointEvidence(False, "compressed_source_reused")
            matched_ids.update(source_ids)
            before_tokens += cls._tokens(source)
            after_tokens += cls._tokens(({**message, "compression": None},))
        return CompressionCheckpointEvidence(True, "matched", before_tokens, after_tokens)

    @staticmethod
    def message_hash(messages: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def text_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _compression_blocks(
        messages: tuple[dict[str, Any], ...],
    ) -> tuple[tuple[tuple[str, ...], dict[str, Any], list[dict[str, Any]]], ...]:
        result = []
        for message in messages:
            compression = message.get("compression")
            source = compression.get("source") if isinstance(compression, dict) else None
            if not isinstance(source, list) or compression.get("deleted") is True:
                continue
            source_ids = tuple(str(item.get("id") or "") for item in source if isinstance(item, dict))
            if source_ids and len(source_ids) == len(source):
                result.append((source_ids, message, source))
        return tuple(result)

    @staticmethod
    def _text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                item if isinstance(item, str) else str(item.get("text") or "")
                for item in content
                if isinstance(item, (str, dict))
            )
        return str(content or "")

    @staticmethod
    def _tokens(messages: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> int:
        return estimate_messages_tokens(deserialize_messages(list(messages), validate=False))
