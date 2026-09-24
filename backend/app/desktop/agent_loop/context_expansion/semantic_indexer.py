r"""本文件对外提供 ProtocolSafeRevisionSegmenter、SegmentSemanticUnitDraft、SemanticProjectionProposal、
SupervisedSegmentSemanticProjector 与 RevisionSemanticIndexer。

输入为冻结 Revision 的完整原始消息序列、来源 identity、content hash、Context 角色及可选受监督 semantic-unit drafts；
输出为完整覆盖且 Tool Exchange 不被拆分的 RevisionSemanticIndex。具体工作流为先把调用及结果折叠为不可分原子组，按有界
消息数生成稳定 segments，再将 worker draft 绑定到精确 NamespacedMessageRef 并验证 confirmed 原文支持，最后构建并校验
版本化 index。示例：`index = RevisionSemanticIndexer().index(source=ref, raw_messages=messages, ...)`。
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    SemanticAuthority,
    SemanticEvidenceUnit,
    SemanticUnitKind,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_index import (
    IndexedMessage,
    RevisionSegment,
    RevisionSemanticIndex,
)
from backend.app.desktop.context_curation import NamespacedMessageRef
from backend.app.desktop.context_evolution import ContextRevisionRef


class SegmentSemanticUnitDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SemanticUnitKind
    authority: SemanticAuthority
    statement: str = Field(min_length=1, max_length=4000)
    message_ids: tuple[str, ...] = Field(min_length=1)


class SemanticProjectionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    units: tuple[SegmentSemanticUnitDraft, ...]


class SupervisedSegmentSemanticProjector:
    VERSION = "supervised-segment-projector-v1"

    def __init__(self, model) -> None:
        self._model = model

    @property
    def attempt_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(getattr(self._model, "last_attempt_records", ()))

    async def project(
        self,
        index: RevisionSemanticIndex,
    ) -> tuple[SegmentSemanticUnitDraft, ...]:
        proposal = await self._model.invoke(
            SemanticProjectionProposal,
            "你是无权 semantic_index_projector。只从输入冻结 segment 原文抽取原子 semantic units，并绑定输入中已有 message_ids；confirmed statement 必须被所引消息直接支持。不得生成 WorkSpec、查询外部历史或执行状态变更。",
            {
                "source": index.source.model_dump(mode="json"),
                "segments": tuple(
                    {
                        "segment": segment.model_dump(mode="json"),
                        "messages": tuple(
                            message.model_dump(mode="json")
                            for message in index.messages
                            if message.message_id in set(segment.message_ids)
                        ),
                    }
                    for segment in index.segments
                ),
            },
        )
        return proposal.units


class ProtocolSafeRevisionSegmenter:
    VERSION = "protocol-safe-segmenter-v1"

    def __init__(self, max_messages: int = 12) -> None:
        if max_messages < 1:
            raise ValueError("segment max_messages 必须为正数")
        self._max_messages = max_messages

    def segment(self, messages: tuple[IndexedMessage, ...]) -> tuple[RevisionSegment, ...]:
        atoms = self._atoms(messages)
        groups: list[tuple[IndexedMessage, ...]] = []
        current: list[IndexedMessage] = []
        for atom in atoms:
            if current and len(current) + len(atom) > self._max_messages:
                groups.append(tuple(current))
                current = []
            current.extend(atom)
        if current:
            groups.append(tuple(current))
        return tuple(self._segment(index, group) for index, group in enumerate(groups))

    def _atoms(self, messages: tuple[IndexedMessage, ...]) -> tuple[tuple[IndexedMessage, ...], ...]:
        atoms: list[tuple[IndexedMessage, ...]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            call_ids = {str(call.get("id") or "") for call in message.tool_calls}
            if not call_ids:
                atoms.append((message,))
                index += 1
                continue
            if "" in call_ids:
                raise ValueError("Assistant tool call 缺少 identity")
            exchange = [message]
            remaining = set(call_ids)
            cursor = index + 1
            while cursor < len(messages) and remaining:
                candidate = messages[cursor]
                exchange.append(candidate)
                remaining.discard(candidate.tool_call_id)
                cursor += 1
            if remaining:
                raise ValueError("Assistant tool call 缺少完整 Tool Result")
            atoms.append(tuple(exchange))
            index = cursor
        return tuple(atoms)

    @staticmethod
    def _segment(ordinal: int, messages: tuple[IndexedMessage, ...]) -> RevisionSegment:
        payload = tuple(item.model_dump(mode="json") for item in messages)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        descriptor_parts = []
        for message in messages:
            content = message.content if isinstance(message.content, str) else json.dumps(message.content, ensure_ascii=False, default=str)
            compact = " ".join(content.split())[:240]
            descriptor_parts.append(f"{message.role}:{compact}" if compact else message.role)
        descriptor = " | ".join(descriptor_parts)[:1200] or "empty-content segment"
        return RevisionSegment.create(
            ordinal=ordinal,
            message_ids=tuple(item.message_id for item in messages),
            descriptor=descriptor,
            content_hash=sha256(encoded.encode("utf-8")).hexdigest(),
        )


class RevisionSemanticIndexer:
    INDEX_SCHEMA_VERSION = "revision-semantic-index-v1"
    PROJECTOR_VERSION = "supervised-segment-projector-v1"

    def __init__(self, segmenter: ProtocolSafeRevisionSegmenter | None = None) -> None:
        self._segmenter = segmenter or ProtocolSafeRevisionSegmenter()

    @property
    def segmenter_version(self) -> str:
        return self._segmenter.VERSION

    def index(
        self,
        *,
        source: ContextRevisionRef,
        source_content_hash: str,
        context_role: str,
        active_objective: str,
        raw_messages: tuple[dict[str, Any], ...],
        unit_drafts: tuple[SegmentSemanticUnitDraft, ...] = (),
    ) -> RevisionSemanticIndex:
        messages = self._messages(raw_messages)
        segments = self._segmenter.segment(messages)
        units = self._units(source, messages, segments, unit_drafts)
        return RevisionSemanticIndex.create(
            source=source,
            source_content_hash=source_content_hash,
            context_role=context_role or "context",
            active_objective=active_objective or "Current mission",
            messages=messages,
            segments=segments,
            semantic_units=units,
            index_schema_version=self.INDEX_SCHEMA_VERSION,
            segmenter_version=self._segmenter.VERSION,
            projector_version=self.PROJECTOR_VERSION,
            protocol_exchange_count=sum(1 for item in messages if item.tool_calls),
        )

    @staticmethod
    def _messages(raw_messages: tuple[dict[str, Any], ...]) -> tuple[IndexedMessage, ...]:
        messages = tuple(
            IndexedMessage(
                message_id=str(item.get("id") or item.get("message_id") or ""),
                ordinal=ordinal,
                role=str(item.get("role") or "unknown"),
                content=item.get("content", ""),
                tool_calls=tuple(item.get("tool_calls") or ()),
                tool_call_id=item.get("tool_call_id"),
            )
            for ordinal, item in enumerate(raw_messages)
        )
        if any(not item.message_id for item in messages):
            raise ValueError("Revision message 缺少 identity")
        if len({item.message_id for item in messages}) != len(messages):
            raise ValueError("Revision message identity 重复")
        return messages

    @staticmethod
    def _units(
        source: ContextRevisionRef,
        messages: tuple[IndexedMessage, ...],
        segments: tuple[RevisionSegment, ...],
        drafts: tuple[SegmentSemanticUnitDraft, ...],
    ) -> tuple[SemanticEvidenceUnit, ...]:
        by_id = {item.message_id: item for item in messages}
        units = []
        for draft in drafts:
            if len(set(draft.message_ids)) != len(draft.message_ids):
                raise ValueError("semantic unit draft message identity 重复")
            unknown = set(draft.message_ids) - set(by_id)
            if unknown:
                raise ValueError("semantic unit draft 引用了 Revision 之外的 message")
            if draft.authority == "confirmed" and not any(
                draft.statement.casefold() in RevisionSemanticIndexer._content(by_id[message_id]).casefold()
                for message_id in draft.message_ids
            ):
                raise ValueError("confirmed semantic unit 无法由所引原文直接支持")
            units.append(
                SemanticEvidenceUnit.create(
                    kind=draft.kind,
                    authority=draft.authority,
                    statement=draft.statement,
                    evidence_refs=tuple(
                        NamespacedMessageRef(source=source, message_id=message_id)
                        for message_id in draft.message_ids
                    ),
                )
            )
        represented = {
            ref.message_id
            for unit in units
            for ref in unit.evidence_refs
            if isinstance(ref, NamespacedMessageRef)
        }
        for segment in segments:
            if set(segment.message_ids).issubset(represented):
                continue
            units.append(
                SemanticEvidenceUnit.create(
                    kind="claim",
                    authority="hypothesis",
                    statement=segment.descriptor,
                    evidence_refs=tuple(
                        NamespacedMessageRef(source=source, message_id=message_id)
                        for message_id in segment.message_ids
                    ),
                )
            )
        return tuple(sorted(units, key=lambda item: item.unit_id))

    @staticmethod
    def _content(message: IndexedMessage) -> str:
        if isinstance(message.content, str):
            return message.content
        return json.dumps(message.content, ensure_ascii=False, sort_keys=True, default=str)
