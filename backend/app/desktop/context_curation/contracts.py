r"""本文件对外提供类型化证据引用、Context frontier、Lane 计划与多来源证据合同。

输入为 Context message、Mission、Run result、Material 或 Workspace effect 的精确版本引用，以及一个或多个
Context revision 证据；输出为严格的 `EvidenceRef`、`MultiSourceEvidence` 与 Lane plan。具体工作流为分别验证
Context lineage 和 evidence frontier，拒绝裸 message id、未知证据类型和越界引用，再把可验证计划交给 compiler。
示例：`plan = LanePlan.model_validate({"action": "create", ...})`。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from backend.app.desktop.context_evolution import ContextRevisionRef


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NamespacedMessageRef(_StrictModel):
    source: ContextRevisionRef
    message_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _require_checkpoint(self) -> Self:
        if not self.source.is_runnable:
            raise ValueError("来源消息必须属于带精确 checkpoint 的 Context revision")
        return self

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.source.context_id,
            self.source.revision_id,
            self.source.checkpoint_id or "",
            self.message_id,
        )


class MissionEvidenceRef(_StrictModel):
    kind: Literal["mission"] = "mission"
    loop_id: str = Field(min_length=1)
    goal_revision: int = Field(ge=1)
    item_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)


class RunResultEvidenceRef(_StrictModel):
    kind: Literal["run_result"] = "run_result"
    run_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)


class MaterialEvidenceRef(_StrictModel):
    kind: Literal["material"] = "material"
    material_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)


class WorkspaceEffectEvidenceRef(_StrictModel):
    kind: Literal["workspace_effect"] = "workspace_effect"
    workspace_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    effect_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)


EvidenceRef = (
    NamespacedMessageRef
    | MissionEvidenceRef
    | RunResultEvidenceRef
    | MaterialEvidenceRef
    | WorkspaceEffectEvidenceRef
)


def evidence_ref_key(ref: EvidenceRef) -> tuple[str, ...]:
    if isinstance(ref, NamespacedMessageRef):
        return ("context_message", *ref.key)
    if isinstance(ref, MissionEvidenceRef):
        return (ref.kind, ref.loop_id, str(ref.goal_revision), ref.item_id, ref.content_hash)
    if isinstance(ref, RunResultEvidenceRef):
        return (ref.kind, ref.run_id, ref.context_id, ref.result_id, ref.content_hash)
    if isinstance(ref, MaterialEvidenceRef):
        return (ref.kind, ref.material_id, ref.version_id, ref.content_hash)
    return (ref.kind, ref.workspace_id, str(ref.revision), ref.effect_id, ref.content_hash)


class SourceMessageEvidence(_StrictModel):
    ref: NamespacedMessageRef
    role: Literal["human", "ai", "system", "tool"]
    content: str | list[Any] = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    status: str | None = None


class SourceRevisionEvidence(_StrictModel):
    source: ContextRevisionRef
    projection_hash: str = Field(min_length=64, max_length=64)
    content_hash: str = Field(min_length=64, max_length=64)
    messages: tuple[SourceMessageEvidence, ...]

    @model_validator(mode="after")
    def _validate_message_namespace(self) -> Self:
        if not self.source.is_runnable:
            raise ValueError("来源 evidence 必须绑定精确 checkpoint")
        keys = [message.ref.key for message in self.messages]
        if len(keys) != len(set(keys)):
            raise ValueError("同一来源 revision 内 message evidence 重复")
        if any(message.ref.source != self.source for message in self.messages):
            raise ValueError("message evidence 与其来源 revision 不一致")
        return self


class StructuredEvidence(_StrictModel):
    ref: MissionEvidenceRef | RunResultEvidenceRef | MaterialEvidenceRef | WorkspaceEffectEvidenceRef
    content: str | list[Any] | dict[str, Any]


class MultiSourceEvidence(_StrictModel):
    sources: tuple[SourceRevisionEvidence, ...] = Field(min_length=1)
    structured: tuple[StructuredEvidence, ...] = ()
    evidence_frontier: tuple[EvidenceRef, ...] = ()

    @model_validator(mode="after")
    def _validate_sources(self) -> Self:
        revision_ids = [source.source.revision_id for source in self.sources]
        if len(revision_ids) != len(set(revision_ids)):
            raise ValueError("来源 revision evidence 重复")
        available = {
            evidence_ref_key(message.ref)
            for source in self.sources
            for message in source.messages
        } | {evidence_ref_key(item.ref) for item in self.structured}
        structured_keys = [evidence_ref_key(item.ref) for item in self.structured]
        if len(structured_keys) != len(set(structured_keys)):
            raise ValueError("结构化 evidence 重复")
        frontier_keys = [evidence_ref_key(ref) for ref in self.evidence_frontier]
        if len(frontier_keys) != len(set(frontier_keys)):
            raise ValueError("evidence frontier 引用重复")
        if frontier_keys and not set(frontier_keys).issubset(available):
            raise ValueError("evidence frontier 引用了不存在的 evidence")
        if self.structured and not frontier_keys:
            raise ValueError("结构化 evidence 必须声明 evidence frontier")
        return self

    def message(self, ref: NamespacedMessageRef) -> SourceMessageEvidence:
        for source in self.sources:
            for message in source.messages:
                if message.ref == ref:
                    return message
        raise KeyError(ref.key)

    def structured_item(self, ref: EvidenceRef) -> StructuredEvidence:
        key = evidence_ref_key(ref)
        for item in self.structured:
            if evidence_ref_key(item.ref) == key:
                return item
        raise KeyError(key)


class CopyMessage(_StrictModel):
    type: Literal["copy_message"]
    source: NamespacedMessageRef


class ComposeMessage(_StrictModel):
    type: Literal["compose_message"]
    role: Literal["system", "human", "ai"]
    content: str = Field(min_length=1)
    sources: tuple[EvidenceRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_sources(self) -> Self:
        keys = [evidence_ref_key(source) for source in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("ComposeMessage 来源 evidence 重复")
        return self


class ToolExchangeCall(_StrictModel):
    name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    result_content: str | list[Any]
    status: Literal["success", "error"] = "success"


class ToolExchange(_StrictModel):
    type: Literal["tool_exchange"]
    assistant_content: str = ""
    calls: tuple[ToolExchangeCall, ...] = Field(min_length=1)
    sources: tuple[NamespacedMessageRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_sources(self) -> Self:
        keys = [source.key for source in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("ToolExchange 来源 evidence 重复")
        return self


LanePlanItem = Annotated[
    CopyMessage | ComposeMessage | ToolExchange,
    Field(discriminator="type"),
]


class _LaneMutation(_StrictModel):
    purpose: str = Field(min_length=1)
    source_frontier: tuple[ContextRevisionRef, ...] = Field(min_length=1)
    evidence_frontier: tuple[EvidenceRef, ...] = ()
    items: tuple[LanePlanItem, ...] = Field(min_length=1)
    lane_policy: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_evidence_frontier(self) -> Self:
        frontier = {
            (
                source.context_id,
                source.revision_id,
                source.checkpoint_id,
            )
            for source in self.source_frontier
        }
        referenced = {
            evidence_ref_key(ref)
            for item in self.items
            for ref in (
                (item.source,)
                if isinstance(item, CopyMessage)
                else item.sources
            )
        }
        referenced_contexts = {
            (
                ref.source.context_id,
                ref.source.revision_id,
                ref.source.checkpoint_id,
            )
            for item in self.items
            for ref in ((item.source,) if isinstance(item, CopyMessage) else item.sources)
            if isinstance(ref, NamespacedMessageRef)
        }
        if not referenced_contexts.issubset(frontier):
            raise ValueError("Lane plan 引用了 source frontier 之外的 evidence")
        if any(not source.is_runnable for source in self.source_frontier):
            raise ValueError("Lane plan source frontier 必须全部绑定精确 checkpoint")
        declared = [evidence_ref_key(ref) for ref in self.evidence_frontier]
        if len(declared) != len(set(declared)):
            raise ValueError("Lane plan evidence frontier 引用重复")
        if declared and not referenced.issubset(set(declared)):
            raise ValueError("Lane plan item 引用了 evidence frontier 之外的 evidence")
        if not declared and any(
            not isinstance(ref, NamespacedMessageRef)
            for item in self.items
            for ref in ((item.source,) if isinstance(item, CopyMessage) else item.sources)
        ):
            raise ValueError("非 Context evidence 必须显式声明 evidence frontier")
        return self


class CreateLanePlan(_LaneMutation):
    action: Literal["create"]
    lane_id: None = None


class UpdateLanePlan(_LaneMutation):
    action: Literal["update"]
    lane_id: str = Field(min_length=1)
    base_context_revision: ContextRevisionRef
    publisher_epoch: int = Field(ge=1)


class KeepLanePlan(_StrictModel):
    action: Literal["keep"]
    lane_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    base_context_revision: ContextRevisionRef
    publisher_epoch: int = Field(ge=1)
    source_frontier: tuple[ContextRevisionRef, ...] = Field(min_length=1)
    semantic_fingerprint: str = Field(min_length=64, max_length=64)


class PauseLanePlan(_StrictModel):
    action: Literal["pause"]
    lane_id: str = Field(min_length=1)
    base_context_revision: ContextRevisionRef
    publisher_epoch: int = Field(ge=1)
    reason: str = Field(min_length=1)


class RetireLanePlan(_StrictModel):
    action: Literal["retire"]
    lane_id: str = Field(min_length=1)
    base_context_revision: ContextRevisionRef
    publisher_epoch: int = Field(ge=1)
    reason: str = Field(min_length=1)


LanePlanValue = Annotated[
    CreateLanePlan | UpdateLanePlan | KeepLanePlan | PauseLanePlan | RetireLanePlan,
    Field(discriminator="action"),
]


class LanePlan(RootModel[LanePlanValue]):
    model_config = ConfigDict(frozen=True)

    @property
    def action(self) -> str:
        return self.root.action


class PortfolioLanePlan(_StrictModel):
    lanes: tuple[LanePlanValue, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_existing_lanes(self) -> Self:
        lane_ids = [lane.lane_id for lane in self.lanes if lane.lane_id is not None]
        if len(lane_ids) != len(set(lane_ids)):
            raise ValueError("Portfolio plan 对同一 Lane 声明了多个操作")
        return self
