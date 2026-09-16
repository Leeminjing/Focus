r"""本文件对外提供 Context revision、版本化来源与执行历史寻址合同。

输入为 Context/revision identity、执行 checkpoint、投影载荷和有序来源；输出为冻结且严格
校验的 `ContextRevisionRef`、`ContextRevisionSourceContract` 与 `ContextRevisionContract`。
具体工作流为先把分散的 thread/namespace/checkpoint 收束为唯一 revision 引用，再由来源合同
组合精确历史版本，最后由完整 revision 合同跨 repository、reader 与 publisher 边界传递。
示例：`config = ContextRevisionRef(...).checkpoint_config()`。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContextRevisionPayloadMode(StrEnum):
    CHECKPOINT = "checkpoint"
    DEFINITION = "definition"


class ContextRevisionProjectionStatus(StrEnum):
    PREPARING = "preparing"
    VALID = "valid"
    REPAIRED = "repaired"
    APPROVED = "approved"
    APPROVAL_REQUIRED = "approval_required"
    REJECTED = "rejected"
    ERROR = "error"
    DELETED = "deleted"


class ContextRevisionOriginKind(StrEnum):
    ROOT = "root"
    MANUAL_DERIVE = "manual_derive"
    DEFINITION_UPDATE = "definition_update"
    PROJECTION_DECISION = "projection_decision"
    COMPRESSION = "compression"
    COMPRESSION_RESTORE = "compression_restore"
    RUN_SETTLED = "run_settled"
    CURATION = "curation"
    MIGRATION = "migration"


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextRevisionRef(_FrozenContract):
    context_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    execution_thread_id: str = Field(min_length=1)
    checkpoint_ns: str = ""
    checkpoint_id: str | None = Field(default=None, min_length=1)
    payload_mode: ContextRevisionPayloadMode

    @model_validator(mode="after")
    def _require_checkpoint_for_checkpoint_payload(self) -> Self:
        if self.payload_mode is ContextRevisionPayloadMode.CHECKPOINT and not self.checkpoint_id:
            raise ValueError("checkpoint-backed revision 必须包含 checkpoint_id")
        return self

    @property
    def is_runnable(self) -> bool:
        return self.checkpoint_id is not None

    def checkpoint_config(self) -> dict[str, dict[str, str]]:
        if self.checkpoint_id is None:
            raise ValueError("尚未准备 checkpoint 的 definition revision 不可寻址执行历史")
        return {
            "configurable": {
                "thread_id": self.execution_thread_id,
                "checkpoint_ns": self.checkpoint_ns,
                "checkpoint_id": self.checkpoint_id,
            }
        }


class ContextRevisionSourceContract(_FrozenContract):
    source: ContextRevisionRef
    position: int = Field(ge=0)

    @model_validator(mode="after")
    def _require_source_checkpoint(self) -> Self:
        if not self.source.is_runnable:
            raise ValueError("revision 来源必须引用已准备的 checkpoint")
        return self


class ContextRevisionContract(_FrozenContract):
    ref: ContextRevisionRef
    sources: tuple[ContextRevisionSourceContract, ...] = ()
    authored_messages: tuple[dict[str, Any], ...] = ()
    execution_messages: tuple[dict[str, Any], ...] = ()
    repair_manifest: tuple[dict[str, Any], ...] = ()
    issues: tuple[dict[str, Any], ...] = ()
    initial_message_ids: tuple[str, ...] = ()
    definition_hash: str | None = Field(default=None, min_length=64, max_length=64)
    projection_hash: str | None = Field(default=None, min_length=64, max_length=64)
    content_hash: str = Field(min_length=64, max_length=64)
    projection_status: ContextRevisionProjectionStatus
    origin_kind: ContextRevisionOriginKind
    origin_id: str | None = Field(default=None, min_length=1)
    created_at: datetime
    deleted_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_source_order(self) -> Self:
        positions = [source.position for source in self.sources]
        if positions != list(range(len(positions))):
            raise ValueError("revision 来源必须按从 0 开始的连续稳定顺序排列")
        if len({source.source.revision_id for source in self.sources}) != len(self.sources):
            raise ValueError("revision 不得重复引用同一来源 revision")
        return self


ContextRevisionMessageViewKind = Literal["authored", "execution", "display"]


class ContextRevisionMessageView(_FrozenContract):
    ref: ContextRevisionRef
    view: ContextRevisionMessageViewKind
    messages: tuple[dict[str, Any], ...] = ()


class ContextRevisionCheckpointView(_FrozenContract):
    ref: ContextRevisionRef
    messages: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextRevisionHistoricalView(_FrozenContract):
    revision: ContextRevisionContract
    authored: ContextRevisionMessageView
    execution: ContextRevisionMessageView
    display: ContextRevisionMessageView
    checkpoint: ContextRevisionCheckpointView | None


class ContextFrontierSummary(_FrozenContract):
    ref: ContextRevisionRef
    projection_status: ContextRevisionProjectionStatus
    content_hash: str = Field(min_length=64, max_length=64)
    summary: str = Field(max_length=280)
    message_count: int = Field(ge=0)
    source_frontier: tuple[ContextRevisionRef, ...] = ()
    deleted: bool = False


class DeletedContextSource(_FrozenContract):
    position: int = Field(ge=0)
    source: ContextRevisionRef
    deleted: bool
    deleted_at: datetime | None = None


class DeletedContextSourceView(_FrozenContract):
    target: ContextRevisionRef
    sources: tuple[DeletedContextSource, ...] = ()


class ContextRevisionPrepareRequest(_FrozenContract):
    context_id: str = Field(min_length=1)
    expected_base: ContextRevisionRef | None = None
    sources: tuple[ContextRevisionSourceContract, ...] = ()
    authored_messages: tuple[dict[str, Any], ...]
    origin_kind: ContextRevisionOriginKind
    origin_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_context_and_sources(self) -> Self:
        if self.expected_base is not None and self.expected_base.context_id != self.context_id:
            raise ValueError("expected_base 必须属于目标 Context")
        positions = [source.position for source in self.sources]
        if positions != list(range(len(positions))):
            raise ValueError("prepare sources 必须按从 0 开始的连续顺序排列")
        return self


class PreparedContextRevision(_FrozenContract):
    expected_base: ContextRevisionRef | None
    revision: ContextRevisionContract

    @property
    def publishable(self) -> bool:
        return (
            self.revision.ref.is_runnable
            and self.revision.projection_status
            in {
                ContextRevisionProjectionStatus.VALID,
                ContextRevisionProjectionStatus.REPAIRED,
                ContextRevisionProjectionStatus.APPROVED,
            }
        )


class ContextRevisionPublicationResult(_FrozenContract):
    previous: ContextRevisionRef | None
    published: ContextRevisionRef


class ContextEvolutionNode(_FrozenContract):
    ref: ContextRevisionRef
    projection_status: ContextRevisionProjectionStatus
    content_hash: str = Field(min_length=64, max_length=64)
    origin_kind: ContextRevisionOriginKind
    origin_id: str | None = None
    current: bool
    deleted: bool


class ContextEvolutionEdge(_FrozenContract):
    target: ContextRevisionRef
    source: ContextRevisionRef
    position: int = Field(ge=0)


class ContextEvolutionGraph(_FrozenContract):
    workspace_id: str = Field(min_length=1)
    nodes: tuple[ContextEvolutionNode, ...] = ()
    edges: tuple[ContextEvolutionEdge, ...] = ()


class ContextFirstParentNode(_FrozenContract):
    context_id: str = Field(min_length=1)
    title: str
    lifecycle: Literal["active", "archived", "deleted"]
    current_revision: ContextRevisionRef | None
    primary_source: ContextRevisionRef | None
    secondary_sources: tuple[ContextRevisionRef, ...] = ()
    tree_parent_context_id: str | None
    depth: int = Field(ge=0)
    cycle_suppressed: bool = False


class ContextFirstParentTree(_FrozenContract):
    workspace_id: str = Field(min_length=1)
    nodes: tuple[ContextFirstParentNode, ...] = ()
