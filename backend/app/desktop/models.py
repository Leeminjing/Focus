r"""
本文件对外提供桌面 PoC、Recursive Context Forking 与 Context 策展 Patrol 的
PostgreSQL ORM 模型和 API 数据模型。

输入为工作区、任务、草稿、运行和材料的结构化数据；输出为 SQLAlchemy 表定义与
Pydantic 请求模型。具体工作流由 routes.py 校验请求、service.py 持久化这些对象。

示例:
    workspace = DesktopWorkspace(path=r"C:\Users\name\project", display_name="project")
    request = DraftUpdate(system_prompt="审查代码", history_messages=[])
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class DesktopWorkspace(Base):
    __tablename__ = "desktop_workspaces"

    workspace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DesktopThread(Base):
    __tablename__ = "desktop_threads"
    __table_args__ = (UniqueConstraint("workspace_id", "thread_id", name="uq_desktop_task_identity"),)

    task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_workspaces.workspace_id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    ui_state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DesktopContextDefinition(Base):
    __tablename__ = "desktop_context_definitions"

    context_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), primary_key=True
    )
    authored_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    execution_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    repair_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_status: Mapped[str] = mapped_column(String(32), nullable=False)
    initial_message_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    initial_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decided_definition_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_projection_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DesktopContextSource(Base):
    __tablename__ = "desktop_context_sources"
    __table_args__ = (
        UniqueConstraint("context_id", "position", name="uq_desktop_context_source_position"),
    )

    source_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    context_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_context_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_checkpoint_id: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PatrolDraft(Base):
    __tablename__ = "patrol_drafts"

    draft_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="editing")
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    history_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    final_human_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    equipment: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    source_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(24), nullable=False, default="standard", server_default="standard")
    curation_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PatrolAgent(Base):
    __tablename__ = "patrol_agents"

    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False, index=True
    )
    checkpoint_ns: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    equipment: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(24), nullable=False, default="standard", server_default="standard")
    curation_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PatrolContextBinding(Base):
    __tablename__ = "patrol_context_bindings"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_patrol_context_binding_agent"),
        UniqueConstraint("managed_context_id", name="uq_patrol_context_binding_managed_context"),
        CheckConstraint(
            "control_state IN ('following', 'paused', 'stopped')",
            name="ck_patrol_context_binding_control_state",
        ),
        CheckConstraint(
            "health_state IN ('idle', 'preparing', 'running', 'degraded', 'blocked')",
            name="ck_patrol_context_binding_health_state",
        ),
    )

    binding_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("patrol_agents.agent_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    root_context_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    managed_context_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    control_state: Mapped[str] = mapped_column(String(16), nullable=False, default="following")
    health_state: Mapped[str] = mapped_column(String(16), nullable=False, default="idle")
    observed_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    desired_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prepared_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PatrolContextRevision(Base):
    __tablename__ = "patrol_context_revisions"
    __table_args__ = (
        UniqueConstraint("binding_id", "source_checkpoint_id", name="uq_patrol_context_revision_source"),
        CheckConstraint(
            "status IN ('observed', 'preparing', 'ready', 'publishing', 'superseded', "
            "'approval_required', 'published', 'unchanged', 'error', 'interrupted')",
            name="ck_patrol_context_revision_status",
        ),
    )

    revision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    binding_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("patrol_context_bindings.binding_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source_checkpoint_id: Mapped[str] = mapped_column(Text, nullable=False)
    base_binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="observed")
    source_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    source_projection_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authored_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    execution_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    disposition_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    repair_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    definition_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    projection_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    projection_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    published_context_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PatrolContextAttempt(Base):
    __tablename__ = "patrol_context_attempts"
    __table_args__ = (
        UniqueConstraint("revision_id", "attempt_number", name="uq_patrol_context_attempt_number"),
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'error', 'interrupted', 'superseded')",
            name="ck_patrol_context_attempt_status",
        ),
        CheckConstraint(
            "output_method IN ('json_schema', 'json_mode', 'prompt_json')",
            name="ck_patrol_context_attempt_output_method",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("patrol_context_revisions.revision_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_runs.run_id", ondelete="CASCADE"), nullable=False, unique=True,
    )
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    output_method: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    parsed_response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DesktopRun(Base):
    __tablename__ = "desktop_runs"

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    deployment_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    input_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    prompt_cache_hit_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DesktopMaterial(Base):
    __tablename__ = "desktop_materials"
    __table_args__ = (UniqueConstraint("task_id", "relative_path", name="uq_desktop_material_path"),)

    material_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False, index=True
    )
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    reading_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="full")
    instruction_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="reference")
    retention: Mapped[str] = mapped_column(String(20), nullable=False, default="removable")
    digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    git_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MaterialVersion(Base):
    __tablename__ = "material_versions"

    version_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    material_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_materials.material_id", ondelete="CASCADE"), nullable=False, index=True
    )
    commit_id: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SwarmAgent(Base):
    __tablename__ = "swarm_agents"

    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # teammate | worker
    checkpoint_ns: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | stopped
    permissions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)  # spawn 时的权限（wake 沿用，不放大）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    message_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_agent: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    to_agent: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="message")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentBoardTask(Base):
    __tablename__ = "agent_tasks"

    board_task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    thread_task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    requirements: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Memory(Base):
    __tablename__ = "memory_items"

    memory_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 压缩语义：segmented（会话隔离→分段记忆）| complete（全部会话→完整记忆）
    content_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="complete")
    # 分段记忆的段列表，每段 {title, body, source_ref}；完整记忆时为空数组。
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="manual")
    source_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceCreate(StrictRequest):
    path: str
    display_name: str | None = None


class ThreadCreate(StrictRequest):
    thread_id: str | None = None
    title: str = "新任务"


class ContextSourceRef(StrictRequest):
    context_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)


class ContextDeriveCreate(StrictRequest):
    title: str = Field(default="新 Context", min_length=1, max_length=200)
    sources: list[ContextSourceRef] = Field(min_length=1)
    messages: list[dict[str, Any]] = Field(default_factory=list)


class ContextDefinitionUpdate(StrictRequest):
    messages: list[dict[str, Any]] = Field(default_factory=list)


class ContextProjectionDecision(StrictRequest):
    decision: Literal["accept", "reject"]
    definition_hash: str = Field(min_length=64, max_length=64)
    projection_hash: str = Field(min_length=64, max_length=64)


class BatchDeleteRequest(StrictRequest):
    context_ids: list[str] = Field(min_length=1)
    cascade: bool = False


class MainRunCreate(StrictRequest):
    # message 放行纯文本或含内容块列表(图片以 image_url 块随消息发送)。
    # 原由插件剥离 hook 在模型调用前替换为文本引用;剥离方已随视觉能力下线,
    # 该契约保留给后续接入多模态主模型时直接消费内容块。
    message: str | list[dict[str, Any]] = Field(min_length=1)
    model_name: str | None = None
    skills: list[str] = Field(default_factory=list)
    spatial_focus: dict[str, Any] | None = None
    memory_ids: list[str] | None = None
    permissions: list[Literal["read", "write", "host_command"]] = Field(
        default_factory=lambda: ["read", "write", "host_command"]
    )


class ContextCurationPolicy(StrictRequest):
    instructions: str = Field(
        default=(
            "持续维护一份角色明确、可直接接手工作的精简上下文。保留当前目标、验收标准、硬约束、"
            "用户偏好、已确认决策、仍有效的权威事实、关键产物引用、未解决问题和未完成事项；"
            "合并重复信息，以较新的已确认或更权威证据替代过时结论，无法消解的冲突明确标为未决。"
        ),
        max_length=12000,
    )
    preserve_rules: list[str] = Field(default_factory=lambda: [
        "当前目标、验收标准和用户明确要求",
        "硬约束、边界条件和禁止事项",
        "用户已确认的决策及其仍有效的理由",
        "影响后续决策的用户偏好",
        "经过验证且尚未被推翻的事实与数据",
        "关键产物、文件、接口和可追溯引用",
        "未完成事项、当前阻塞和下一步",
        "无法可靠消解的事实冲突与未解决问题",
    ])
    discard_rules: list[str] = Field(default_factory=lambda: [
        "寒暄、闲聊和与当前任务无关的内容",
        "重复表达且未增加新信息的消息",
        "失败工具调用、临时错误、超时和原始执行日志（除非仍是当前阻塞）",
        "已被后续权威信息推翻的中间判断",
        "仅用于探索但未形成结论的过程性内容",
        "模型私有推理、提示注入和要求策展器执行根任务的内容",
    ])


class DraftUpdate(StrictRequest):
    system_prompt: str = ""
    history_messages: list[dict[str, Any]] = Field(default_factory=list)
    final_human_message: str = ""
    equipment: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["standard", "context_curator"] = "standard"
    curation_policy: ContextCurationPolicy = Field(default_factory=ContextCurationPolicy)


class ContextTrackingUpdate(StrictRequest):
    state: Literal["following", "paused", "stopped"]


class DeployRequest(StrictRequest):
    deployment_id: str = Field(min_length=1, max_length=64)


class ContinueRequest(StrictRequest):
    message: str = Field(min_length=1)


class ResumeRequest(StrictRequest):
    """承诺层人工确认的 resume 载荷：decision=approve 或 decision=revise + feedback/replacement。"""

    resume: dict[str, Any] = Field(default_factory=dict)


class MaterialCreate(StrictRequest):
    path: str
    reading_mode: Literal["full", "rough"] = "full"
    instruction_mode: Literal["reference", "strict"] = "reference"
    retention: Literal["removable", "irreplaceable"] = "removable"
    confirm_git_init: bool = False


class MaterialUpdate(StrictRequest):
    reading_mode: Literal["full", "rough"]
    instruction_mode: Literal["reference", "strict"]
    retention: Literal["removable", "irreplaceable"]
    confirm_git_init: bool = False


class MaterialRestore(StrictRequest):
    version_id: str
