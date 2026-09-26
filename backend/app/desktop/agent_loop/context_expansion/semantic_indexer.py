r"""本文件对外提供 ProtocolSafeRevisionSegmenter、语义投影合同、SupervisedSegmentSemanticProjector 与 RevisionSemanticIndexer。

输入为冻结 Revision 的完整原始消息序列、来源 identity、content hash、Context 角色及可选受监督 semantic-unit drafts；
输出为完整覆盖且 Tool Exchange 不被拆分的 RevisionSemanticIndex。具体工作流为先把调用及结果折叠为不可分原子组，按有界
消息数生成稳定 segments，再委托 grounding validator 独立验证 support spans 与 claim verdict，隔离无效 unit 并用 segment fallback
补足覆盖，最后构建版本化 index。示例：`index = RevisionSemanticIndexer().index(source=ref, raw_messages=messages, ...)`。
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from backend.app.desktop.agent_loop.context_expansion.contracts import SemanticEvidenceUnit
from backend.app.desktop.agent_loop.context_expansion.semantic_grounding import (
    SegmentSemanticUnitDraft,
    SemanticClaimSupportAssessment,
    SemanticGroundingError,
    SemanticGroundingValidator,
    SemanticProjectionProposal,
    SemanticUnitRejection,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_index import (
    IndexedMessage,
    RevisionSegment,
    RevisionSemanticIndex,
)
from backend.app.desktop.agent_loop.derivation_worker import StructuredResultValidationError
from backend.app.desktop.context_curation import NamespacedMessageRef
from backend.app.desktop.context_evolution import ContextRevisionRef


class SupervisedSegmentSemanticProjector:
    VERSION = "supervised-segment-projector-v2"

    def __init__(self, model) -> None:
        self._model = model

    @property
    def attempt_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(getattr(self._model, "last_attempt_records", ()))

    async def project(
        self,
        index: RevisionSemanticIndex,
    ) -> tuple[SegmentSemanticUnitDraft, ...]:
        system = "你是无权 semantic_index_projector。只从输入冻结 segment 原文抽取原子 semantic units。每个 unit 分别输出自然语言 statement 与一个或多个 supports；每个 support 的 message_id 必须来自输入，quote 必须逐字复制该消息原文。confirmed statement 必须被全部引文共同直接支持。不得生成 WorkSpec、查询外部历史或执行状态变更。"
        payload = {
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
            }
        invoke_validated = getattr(self._model, "invoke_validated", None)
        if invoke_validated is None:
            proposal = await self._model.invoke(SemanticProjectionProposal, system, payload)
            self._validate_batch(proposal)
        else:
            proposal = await invoke_validated(
                SemanticProjectionProposal,
                system,
                payload,
                self._validate_batch,
            )
        return proposal.units

    @staticmethod
    def _validate_batch(proposal: SemanticProjectionProposal) -> None:
        seen: set[str] = set()
        for unit in proposal.units:
            if unit.claim_key in seen:
                raise StructuredResultValidationError(
                    "semantic_support_error",
                    "semantic projector 返回了重复的 unit identity",
                    unit_identity=unit.claim_key,
                    violated_rule="each projected semantic unit identity must be unique within one response",
                )
            seen.add(unit.claim_key)


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
    INDEX_SCHEMA_VERSION = "revision-semantic-index-v3"
    PROJECTOR_VERSION = "supervised-segment-projector-v2"

    def __init__(
        self,
        segmenter: ProtocolSafeRevisionSegmenter | None = None,
        grounding_validator: SemanticGroundingValidator | None = None,
    ) -> None:
        self._segmenter = segmenter or ProtocolSafeRevisionSegmenter()
        self._grounding = grounding_validator or SemanticGroundingValidator()

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
        claim_support_assessments: tuple[SemanticClaimSupportAssessment, ...] = (),
    ) -> RevisionSemanticIndex:
        messages = self._messages(raw_messages)
        segments = self._segmenter.segment(messages)
        units, rejections, projected_ids, fallback_ids = self._units(
            source,
            messages,
            segments,
            unit_drafts,
            claim_support_assessments,
        )
        return RevisionSemanticIndex.create(
            source=source,
            source_content_hash=source_content_hash,
            context_role=context_role or "context",
            active_objective=active_objective or "Current mission",
            messages=messages,
            segments=segments,
            semantic_units=units,
            rejected_units=rejections,
            projected_unit_ids=projected_ids,
            fallback_segment_ids=fallback_ids,
            quality_state="degraded" if rejections or fallback_ids else "complete",
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

    def _units(
        self,
        source: ContextRevisionRef,
        messages: tuple[IndexedMessage, ...],
        segments: tuple[RevisionSegment, ...],
        drafts: tuple[SegmentSemanticUnitDraft, ...],
        assessments: tuple[SemanticClaimSupportAssessment, ...],
    ) -> tuple[
        tuple[SemanticEvidenceUnit, ...],
        tuple[SemanticUnitRejection, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]:
        contents = self.message_contents(messages)
        by_claim = {assessment.claim_key: assessment for assessment in assessments}
        if len(by_claim) != len(assessments):
            raise ValueError("claim support assessment identity 重复")
        units: list[SemanticEvidenceUnit] = []
        rejections: list[SemanticUnitRejection] = []
        projected_ids: list[str] = []
        fallback_ids: list[str] = []
        for draft in drafts:
            try:
                unit = self._grounding.validate(
                    source,
                    contents,
                    draft,
                    by_claim.get(draft.claim_key),
                )
                units.append(unit)
                projected_ids.append(unit.unit_id)
            except SemanticGroundingError as exc:
                rejections.append(
                    SemanticUnitRejection.create(
                        draft,
                        code=exc.code,
                        summary=exc.summary,
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
            fallback_ids.append(segment.segment_id)
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
        return (
            tuple(sorted(units, key=lambda item: item.unit_id)),
            tuple(sorted(rejections, key=lambda item: item.rejection_id)),
            tuple(sorted(projected_ids)),
            tuple(sorted(fallback_ids)),
        )

    @staticmethod
    def message_content(message: IndexedMessage) -> str:
        if isinstance(message.content, str):
            return message.content
        return json.dumps(message.content, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def message_contents(messages: tuple[IndexedMessage, ...]) -> dict[str, str]:
        return {
            message.message_id: RevisionSemanticIndexer.message_content(message)
            for message in messages
        }
