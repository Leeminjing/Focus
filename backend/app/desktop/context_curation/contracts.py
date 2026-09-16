r"""本文件对外提供 frontier、Lane 计划与命名空间来源证据合同。

输入为一个或多个 Context revision 证据及 Lane 目的；输出为严格的策展计划与候选模型。
具体工作流为拒绝裸 message id 和未知 action，并把可验证计划交给 compiler。
示例：`plan = LanePlan.model_validate(payload)`。
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


class MultiSourceEvidence(_StrictModel):
    sources: tuple[SourceRevisionEvidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_sources(self) -> Self:
        revision_ids = [source.source.revision_id for source in self.sources]
        if len(revision_ids) != len(set(revision_ids)):
            raise ValueError("来源 revision evidence 重复")
        return self

    def message(self, ref: NamespacedMessageRef) -> SourceMessageEvidence:
        for source in self.sources:
            for message in source.messages:
                if message.ref == ref:
                    return message
        raise KeyError(ref.key)


class CopyMessage(_StrictModel):
    type: Literal["copy_message"]
    source: NamespacedMessageRef


class ComposeMessage(_StrictModel):
    type: Literal["compose_message"]
    role: Literal["system", "human", "ai"]
    content: str = Field(min_length=1)
    sources: tuple[NamespacedMessageRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_sources(self) -> Self:
        keys = [source.key for source in self.sources]
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
            (
                ref.source.context_id,
                ref.source.revision_id,
                ref.source.checkpoint_id,
            )
            for item in self.items
            for ref in (
                (item.source,)
                if isinstance(item, CopyMessage)
                else item.sources
            )
        }
        if not referenced.issubset(frontier):
            raise ValueError("Lane plan 引用了 source frontier 之外的 evidence")
        if any(not source.is_runnable for source in self.source_frontier):
            raise ValueError("Lane plan source frontier 必须全部绑定精确 checkpoint")
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
