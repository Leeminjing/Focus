"""本文件对外提供 TaskProgressDocument、贡献、来源清单和冻结决策输入合同。

输入为稳定任务身份、类型化领域证据和不可变来源版本；输出为禁止额外字段的冻结模型。
具体工作流为 schema 校验来源身份、修正关系和支持程度，再以 canonical_hash 绑定内容。
Tool、Message、Checkpoint 定位只属于独立审计记录，不是任务条目类型。
示例：TaskItem(item_id="check:tests", description="测试通过", state="not_started")。
全部来源须有变化或显式来源解释，unknown 保留未决；CandidateIssue 只承载安全错误码、路径和引用投影；Lineage 验证完整节点/边身份及可重算拓扑 hash。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.desktop.context_evolution.lineage_contracts import LineageSnapshot
from backend.app.desktop.domain_evidence.identity import canonical_hash


class MemoryContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskSource(MemoryContract):
    source_key: str = Field(min_length=1)
    kind: Literal["run_outcome", "test", "workspace", "artifact", "user_revision"]
    source_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    context_id: str | None = None
    run_id: str | None = None
    execution_round_id: str | None = None
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _require_identity(self):
        if self.source_key != canonical_hash([self.kind, self.source_id, self.version]):
            raise ValueError("任务来源身份未绑定 kind/id/version")
        if self.version != canonical_hash(self.payload):
            raise ValueError("任务来源版本未绑定领域 payload")
        return self


class TaskDeltaManifest(MemoryContract):
    boundary: str
    complete: bool = True
    sources: tuple[TaskSource, ...] = ()
    blocker: str | None = None

    @model_validator(mode="after")
    def _unique_sources(self):
        if len({item.source_key for item in self.sources}) != len(self.sources):
            raise ValueError("冻结来源重复")
        if not self.complete and not self.blocker:
            raise ValueError("不完整来源必须说明 blocker")
        return self


class TaskItem(MemoryContract):
    item_id: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=6000)
    context_ids: tuple[str, ...] = ()
    state: Literal[
        "not_started",
        "in_progress",
        "completed",
        "blocked",
        "unknown",
        "conflicted",
        "not_applicable",
    ] = "unknown"
    support: Literal["unknown", "asserted", "supported"] = "unknown"
    evidence_keys: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    corrects: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _require_support(self):
        if (
            self.support == "supported" or self.corrects or self.supersedes
        ) and not self.evidence_keys:
            raise ValueError("支持或修正结论必须有领域证据")
        if self.state == "completed" and self.support != "supported":
            raise ValueError("任务完成必须有独立领域支持")
        return self


class TaskProgressDocument(MemoryContract):
    schema_version: Literal[1] = 1
    mission_revision: int = Field(ge=1)
    items: tuple[TaskItem, ...] = ()
    baseline_kind: Literal["initial", "migration", "round"] = "initial"
    history_complete: bool = True

    @model_validator(mode="after")
    def _unique_items(self):
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("任务事项 identity 重复")
        return self


class SourceAssessment(MemoryContract):
    source_key: str
    disposition: Literal["already_known", "not_task_progress", "unknown"]
    explanation: str = Field(min_length=1, max_length=2000)


class RunContribution(MemoryContract):
    run_id: str
    execution_round_id: str | None = None
    observed_round_id: str
    source_keys: tuple[str, ...]
    changes: tuple[TaskItem, ...] = ()
    source_assessments: tuple[SourceAssessment, ...] = ()


class RoundContribution(MemoryContract):
    round_id: str
    run_contributions: tuple[RunContribution, ...] = ()
    direct_source_keys: tuple[str, ...] = ()
    changes: tuple[TaskItem, ...] = ()
    source_assessments: tuple[SourceAssessment, ...] = ()


class ProgressCandidate(MemoryContract):
    changes: tuple[TaskItem, ...] = ()
    source_assessments: tuple[SourceAssessment, ...] = ()


class CandidateIssue(MemoryContract):
    code: str
    path: tuple[str | int, ...]
    references: tuple[dict[str, Any], ...] = ()


class RoundDecisionInputs(MemoryContract):
    schema_version: Literal[1] = 1
    round_id: str
    observation_id: str
    observation_hash: str
    previous_progress_id: str
    previous_progress_hash: str
    previous_progress: TaskProgressDocument
    task_delta: TaskDeltaManifest
    manifest_hash: str
    lineage: dict[str, Any]
    topology_hash: str

    @model_validator(mode="after")
    def _require_hashes(self):
        if canonical_hash(self.previous_progress) != self.previous_progress_hash:
            raise ValueError("前序进度 hash 不一致")
        if canonical_hash(self.task_delta) != self.manifest_hash:
            raise ValueError("冻结增量 hash 不一致")
        if self.lineage.get("topology_hash") != self.topology_hash:
            raise ValueError("冻结 Lineage hash 不一致")
        LineageSnapshot.model_validate(self.lineage)
        return self
