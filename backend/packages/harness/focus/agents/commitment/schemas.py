"""
本文件对外提供承诺层的 Pydantic 数据模型和 CommitmentState 图状态。

输入:
    各阶段产生的目标、要求、兼容性、文件、网址、技术版本、理由摘要及审核结果数据。

输出:
    TaskEnvelope — Supervisor 传给 delegate_with_review 的固定输入。
    WorkerOutput / ReviewOutput — Worker 产物与 Evaluator 审核结论。
    CommitmentState — 九阶段 Supervisor 子图共享的状态结构。

具体工作流:
    (1) 使用字段类型和约束声明阶段数据边界。
    (2) Worker、Evaluator 和 Supervisor 通过相同模型交换结构化数据。
    (3) LangGraph 使用 CommitmentState 保存阶段、人工等待、冻结知识、第七阶段批准证明和最终合同。
        父图复用该 schema 的 task_contract_source 区分 typed 镜像与 legacy 字符串；旧 checkpoint 不要求该字段。

示例:
    envelope = TaskEnvelope(stage=1, instruction="明确目标")
"""

from typing import Any, Literal

from langchain.agents.middleware.types import AgentState
from pydantic import BaseModel, Field
from typing_extensions import Annotated, NotRequired

from focus.history.bridge import replace_execution_items


class TaskEnvelope(BaseModel):
    stage: int = Field(ge=1, le=9)
    instruction: str
    context: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)


class WorkerOutput(BaseModel):
    result: dict[str, Any]
    artifact_ref: str | None = None
    reasoning_summary: str = ""


class ReviewOutput(BaseModel):
    approved: bool
    feedback: str = ""
    reasoning_summary: str = ""


class CompatibilityCheck(BaseModel):
    technology: str
    application_type: str
    ui_surface: str
    runtime_platform: str
    host_model: str
    status: Literal["verified", "conflict", "unresolved"]


class RequirementConflict(BaseModel):
    requirements: list[str] = Field(min_length=1)
    conflict_type: str
    explanation: str
    status: Literal["open", "resolved"]
    resolution: str | None = None


class StageTwoResult(BaseModel):
    requirements: list[str] = Field(min_length=1)
    discarded_requirements: list[str] = Field(default_factory=list)
    compatibility_checks: list[CompatibilityCheck] = Field(min_length=1)
    conflicts: list[RequirementConflict]


class FileReference(BaseModel):
    mention: str
    uploaded_filename: str | None = None
    candidates: list[str] = Field(default_factory=list)
    status: Literal["matched", "proposed", "unresolved"]


class UrlReference(BaseModel):
    mention: str
    url: str | None = None
    candidates: list[str] = Field(default_factory=list)
    source: Literal["user", "search", "none"]
    status: Literal["provided", "proposed", "unresolved"]


class StageFourResult(BaseModel):
    files: list[FileReference]
    urls: list[UrlReference]


class TechnologySelection(BaseModel):
    technologies: list[str] = Field(min_length=1)


class TechnologyVersion(BaseModel):
    name: str
    project_version: str
    version: str
    library_id: str | None = None
    source_url: str | None = None
    version_basis: Literal[
        "official_docs_explicit",
        "context7_version_list",
        "latest_stable_policy",
        "unresolved",
    ] = "unresolved"
    version_evidence: str | None = None


class StageFiveResult(BaseModel):
    technologies: list[TechnologyVersion] = Field(min_length=1)


class CommitmentState(AgentState):
    execution_items: Annotated[list[dict] | None, replace_execution_items]
    stage: NotRequired[int]
    awaiting_human: NotRequired[int | None]
    artifacts: NotRequired[dict[str, Any]]
    source_text: NotRequired[str]
    handoff_source_refs: NotRequired[list[dict[str, Any]]]
    uploads_tag: NotRequired[str]
    thread_id: NotRequired[str]
    workspace: NotRequired[str]
    knowledge_files: NotRequired[list[str]]
    knowledge_snapshots: NotRequired[list[dict[str, Any]]]
    contract_approval: NotRequired[dict[str, Any]]
    task_contract: NotRequired[str | None]
    task_contract_source: NotRequired[Literal["typed", "legacy"]]
    final_message: NotRequired[str]
