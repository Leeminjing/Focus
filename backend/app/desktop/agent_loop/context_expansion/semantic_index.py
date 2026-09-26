r"""本文件对外提供完整 Revision semantic index、质量状态、rejection ledger 与稳定 identity 工具。

输入为冻结 ContextRevisionRef、Revision content hash、按原始顺序排列的 message identities、protocol-safe segments
与经验证的 semantic units；输出为可重放的 RevisionSemanticIndex、RevisionIndexDescriptor 和覆盖账本。具体工作流为
规范化 descriptor，验证消息恰好被一个 segment 覆盖、Tool Exchange 不跨 segment、semantic unit 只引用本 Revision，
记录 accepted 投影、segment fallback、complete/degraded 状态与隔离原因，再由来源与版本化 payload 计算 index identity。示例：
`index = RevisionSemanticIndex.create(...)`。
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.desktop.agent_loop.context_expansion.contracts import (
    SemanticEvidenceUnit,
    stable_expansion_hash,
)
from backend.app.desktop.agent_loop.context_expansion.semantic_grounding import SemanticUnitRejection
from backend.app.desktop.context_curation import NamespacedMessageRef
from backend.app.desktop.context_evolution import ContextRevisionRef


class _IndexModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IndexedMessage(_IndexModel):
    message_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    role: str = Field(min_length=1)
    content: Any = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    tool_call_id: str | None = None


class RevisionSegment(_IndexModel):
    segment_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordinal: int = Field(ge=0)
    message_ids: tuple[str, ...] = Field(min_length=1)
    descriptor: str = Field(min_length=1, max_length=1200)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_stable_identity(self) -> Self:
        if len(self.message_ids) != len(set(self.message_ids)):
            raise ValueError("segment message identity 重复")
        expected = stable_expansion_hash(
            "revision-segment",
            self.ordinal,
            self.message_ids,
            self.descriptor,
            self.content_hash,
        )
        if self.segment_id != expected:
            raise ValueError("segment identity 与内容不一致")
        return self

    @classmethod
    def create(
        cls,
        *,
        ordinal: int,
        message_ids: tuple[str, ...],
        descriptor: str,
        content_hash: str,
    ) -> Self:
        return cls(
            segment_id=stable_expansion_hash(
                "revision-segment",
                ordinal,
                message_ids,
                descriptor,
                content_hash,
            ),
            ordinal=ordinal,
            message_ids=message_ids,
            descriptor=descriptor,
            content_hash=content_hash,
        )


class RevisionCoverageLedger(_IndexModel):
    message_ids: tuple[str, ...]
    segment_ids: tuple[str, ...]
    covered_message_count: int = Field(ge=0)
    protocol_exchange_count: int = Field(ge=0)

    @model_validator(mode="after")
    def require_unique_coverage_inventory(self) -> Self:
        if len(self.message_ids) != len(set(self.message_ids)):
            raise ValueError("coverage ledger message identity 重复")
        if len(self.segment_ids) != len(set(self.segment_ids)):
            raise ValueError("coverage ledger segment identity 重复")
        if self.covered_message_count != len(self.message_ids):
            raise ValueError("coverage ledger count 与 message inventory 不一致")
        return self


class RevisionIndexDescriptor(_IndexModel):
    index_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: ContextRevisionRef
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_role: str = Field(min_length=1, max_length=120)
    active_objective: str = Field(min_length=1, max_length=2000)
    segment_count: int = Field(ge=0)
    semantic_unit_count: int = Field(ge=0)
    index_schema_version: str = Field(min_length=1, max_length=64)
    segmenter_version: str = Field(min_length=1, max_length=64)
    projector_version: str = Field(min_length=1, max_length=64)
    quality_state: Literal["complete", "degraded"] = "complete"
    rejected_unit_count: int = Field(default=0, ge=0)
    projected_unit_count: int = Field(default=0, ge=0)
    fallback_segment_count: int = Field(default=0, ge=0)


class RevisionSemanticIndex(_IndexModel):
    index_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: ContextRevisionRef
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_role: str = Field(min_length=1, max_length=120)
    active_objective: str = Field(min_length=1, max_length=2000)
    messages: tuple[IndexedMessage, ...]
    segments: tuple[RevisionSegment, ...]
    semantic_units: tuple[SemanticEvidenceUnit, ...] = ()
    rejected_units: tuple[SemanticUnitRejection, ...] = ()
    projected_unit_ids: tuple[str, ...] = ()
    fallback_segment_ids: tuple[str, ...] = ()
    quality_state: Literal["complete", "degraded"] = "complete"
    coverage: RevisionCoverageLedger
    index_schema_version: str = Field(min_length=1, max_length=64)
    segmenter_version: str = Field(min_length=1, max_length=64)
    projector_version: str = Field(min_length=1, max_length=64)

    @field_validator("messages", mode="before")
    @classmethod
    def order_messages(cls, values: Any) -> tuple[Any, ...]:
        return tuple(sorted(values or (), key=lambda item: item.get("ordinal", 0) if isinstance(item, dict) else item.ordinal))

    @field_validator("segments", mode="before")
    @classmethod
    def order_segments(cls, values: Any) -> tuple[Any, ...]:
        return tuple(sorted(values or (), key=lambda item: item.get("ordinal", 0) if isinstance(item, dict) else item.ordinal))

    @model_validator(mode="after")
    def require_complete_valid_index(self) -> Self:
        if not self.source.is_runnable:
            raise ValueError("Revision semantic index source 必须是可精确读取的 runnable Revision")
        message_ids = tuple(item.message_id for item in self.messages)
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("Revision index message identity 重复")
        if tuple(item.ordinal for item in self.messages) != tuple(range(len(self.messages))):
            raise ValueError("Revision index message ordinal 不连续")
        covered = tuple(message_id for segment in self.segments for message_id in segment.message_ids)
        if covered != message_ids:
            raise ValueError("Revision index segments 未按原序恰好覆盖全部消息")
        if self.coverage.message_ids != message_ids:
            raise ValueError("Revision index coverage ledger 与消息不一致")
        if self.coverage.segment_ids != tuple(item.segment_id for item in self.segments):
            raise ValueError("Revision index coverage ledger 与 segments 不一致")
        self._require_semantic_provenance(set(message_ids))
        self._require_protocol_closure()
        if self.index_id != self._identity():
            raise ValueError("Revision semantic index identity 与冻结 payload 不一致")
        return self

    def _require_semantic_provenance(self, known_message_ids: set[str]) -> None:
        unit_ids = [item.unit_id for item in self.semantic_units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("Revision index semantic unit identity 重复")
        rejection_ids = [item.rejection_id for item in self.rejected_units]
        if len(rejection_ids) != len(set(rejection_ids)):
            raise ValueError("Revision index semantic rejection identity 重复")
        projected = set(self.projected_unit_ids)
        fallback = set(self.fallback_segment_ids)
        if len(projected) != len(self.projected_unit_ids) or not projected.issubset(unit_ids):
            raise ValueError("projected unit inventory 与 semantic units 不一致")
        segment_ids = {item.segment_id for item in self.segments}
        if len(fallback) != len(self.fallback_segment_ids) or not fallback.issubset(segment_ids):
            raise ValueError("fallback segment inventory 与 segments 不一致")
        if self.quality_state == "complete" and (self.rejected_units or fallback):
            raise ValueError("complete semantic index 不得包含 rejected units 或 segment fallback")
        if self.quality_state == "degraded" and not (self.rejected_units or fallback):
            raise ValueError("degraded semantic index 必须包含 rejected units 或 segment fallback")
        for unit in self.semantic_units:
            for ref in unit.evidence_refs:
                if not isinstance(ref, NamespacedMessageRef):
                    raise TypeError("Revision index semantic unit 只能引用 message evidence")
                if ref.source != self.source or ref.message_id not in known_message_ids:
                    raise ValueError("Revision index semantic unit 引用了 index 之外的 message")
        self._require_fallback_inventory(projected, fallback, set(unit_ids))

    def _require_fallback_inventory(
        self,
        projected: set[str],
        fallback: set[str],
        unit_ids: set[str],
    ) -> None:
        represented = {
            ref.message_id
            for unit in self.semantic_units
            if unit.unit_id in projected
            for ref in unit.evidence_refs
        }
        expected_fallback = {
            segment.segment_id
            for segment in self.segments
            if not set(segment.message_ids).issubset(represented)
        }
        if fallback != expected_fallback:
            raise ValueError("fallback segment inventory 未精确覆盖未投影的来源")
        expected_fallback_units = {
            SemanticEvidenceUnit.create(
                kind="claim",
                authority="hypothesis",
                statement=segment.descriptor,
                evidence_refs=tuple(
                    NamespacedMessageRef(source=self.source, message_id=message_id)
                    for message_id in segment.message_ids
                ),
            ).unit_id
            for segment in self.segments
            if segment.segment_id in fallback
        }
        if unit_ids != projected | expected_fallback_units:
            raise ValueError("semantic units 与 projected/fallback inventory 不一致")

    def descriptor(self) -> RevisionIndexDescriptor:
        return RevisionIndexDescriptor(
            index_id=self.index_id,
            source=self.source,
            source_content_hash=self.source_content_hash,
            context_role=self.context_role,
            active_objective=self.active_objective,
            segment_count=len(self.segments),
            semantic_unit_count=len(self.semantic_units),
            index_schema_version=self.index_schema_version,
            segmenter_version=self.segmenter_version,
            projector_version=self.projector_version,
            quality_state=self.quality_state,
            rejected_unit_count=len(self.rejected_units),
            projected_unit_count=len(self.projected_unit_ids),
            fallback_segment_count=len(self.fallback_segment_ids),
        )

    def _require_protocol_closure(self) -> None:
        segment_by_message = {
            message_id: segment.segment_id
            for segment in self.segments
            for message_id in segment.message_ids
        }
        calls: dict[str, str] = {}
        for message in self.messages:
            for call in message.tool_calls:
                call_id = str(call.get("id") or "")
                if not call_id or call_id in calls:
                    raise ValueError("Tool Exchange call identity 缺失或重复")
                calls[call_id] = message.message_id
            if message.tool_call_id:
                assistant_id = calls.get(message.tool_call_id)
                if assistant_id is None:
                    raise ValueError("Tool Result 缺少同 Revision 的 Assistant tool call")
                if segment_by_message[assistant_id] != segment_by_message[message.message_id]:
                    raise ValueError("Tool Exchange 被拆分到不同 segments")
        returned_ids = [item.tool_call_id for item in self.messages if item.tool_call_id]
        if len(returned_ids) != len(set(returned_ids)):
            raise ValueError("Tool Exchange Result identity 重复")
        returned = set(returned_ids)
        if set(calls) != returned:
            raise ValueError("Assistant tool call 缺少对应 Tool Result")

    def _identity(self) -> str:
        return stable_expansion_hash(
            "revision-semantic-index",
            self.source.model_dump(mode="json"),
            self.source_content_hash,
            self.index_schema_version,
            self.segmenter_version,
            self.projector_version,
            tuple(item.model_dump(mode="json") for item in self.messages),
            tuple(item.model_dump(mode="json") for item in self.segments),
            tuple(item.model_dump(mode="json") for item in self.semantic_units),
            tuple(item.model_dump(mode="json") for item in self.rejected_units),
            self.projected_unit_ids,
            self.fallback_segment_ids,
            self.quality_state,
        )

    @classmethod
    def create(
        cls,
        *,
        source: ContextRevisionRef,
        source_content_hash: str,
        context_role: str,
        active_objective: str,
        messages: tuple[IndexedMessage, ...],
        segments: tuple[RevisionSegment, ...],
        semantic_units: tuple[SemanticEvidenceUnit, ...] = (),
        rejected_units: tuple[SemanticUnitRejection, ...] = (),
        projected_unit_ids: tuple[str, ...] = (),
        fallback_segment_ids: tuple[str, ...] = (),
        quality_state: Literal["complete", "degraded"] = "complete",
        index_schema_version: str,
        segmenter_version: str,
        projector_version: str,
        protocol_exchange_count: int = 0,
    ) -> Self:
        ordered_messages = tuple(sorted(messages, key=lambda item: item.ordinal))
        ordered_segments = tuple(sorted(segments, key=lambda item: item.ordinal))
        ordered_units = tuple(sorted(semantic_units, key=lambda item: item.unit_id))
        ordered_rejections = tuple(sorted(rejected_units, key=lambda item: item.rejection_id))
        ordered_projected_ids = tuple(sorted(projected_unit_ids))
        ordered_fallback_ids = tuple(sorted(fallback_segment_ids))
        coverage = RevisionCoverageLedger(
            message_ids=tuple(item.message_id for item in ordered_messages),
            segment_ids=tuple(item.segment_id for item in ordered_segments),
            covered_message_count=len(ordered_messages),
            protocol_exchange_count=protocol_exchange_count,
        )
        payload = (
            source.model_dump(mode="json"),
            source_content_hash,
            index_schema_version,
            segmenter_version,
            projector_version,
            tuple(item.model_dump(mode="json") for item in ordered_messages),
            tuple(item.model_dump(mode="json") for item in ordered_segments),
            tuple(item.model_dump(mode="json") for item in ordered_units),
            tuple(item.model_dump(mode="json") for item in ordered_rejections),
            ordered_projected_ids,
            ordered_fallback_ids,
            quality_state,
        )
        return cls(
            index_id=stable_expansion_hash("revision-semantic-index", *payload),
            source=source,
            source_content_hash=source_content_hash,
            context_role=context_role,
            active_objective=active_objective,
            messages=ordered_messages,
            segments=ordered_segments,
            semantic_units=ordered_units,
            rejected_units=ordered_rejections,
            projected_unit_ids=ordered_projected_ids,
            fallback_segment_ids=ordered_fallback_ids,
            quality_state=quality_state,
            coverage=coverage,
            index_schema_version=index_schema_version,
            segmenter_version=segmenter_version,
            projector_version=projector_version,
        )


IndexEntryKind = Literal["segment", "semantic_unit"]
