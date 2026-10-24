"""本文件对外提供桌面工作区、任务、Run、材料、协作与 Memory 的 ORM 定义。

输入为 SQLAlchemy 声明；输出为注册在共享 Base.metadata 中的持久实体。
具体工作流由 persistence.registry 汇集各领域模型，表名、列、约束及默认值沿用既有迁移。
请求校验合同位于 api.schemas；标题上限与装备字段名由 persistence.contracts 共享。
示例：session.get(DesktopRun, run_id)。
"""

from datetime import datetime
from typing import Any

from focus.persistence.base import Base
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.desktop.persistence.contracts import (
    THREAD_TITLE_LIMIT,
)


class DesktopWorkspace(Base):
    __tablename__ = "desktop_workspaces"

    workspace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DesktopThread(Base):
    __tablename__ = "desktop_threads"
    __table_args__ = (
        UniqueConstraint("workspace_id", "thread_id", name="uq_desktop_task_identity"),
    )

    task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
    )
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey(
            "desktop_context_revisions.revision_id",
            name="fk_desktop_thread_current_revision",
            ondelete="SET NULL",
            use_alter=True,
        ),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(THREAD_TITLE_LIMIT), nullable=False)
    ui_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PatrolDraft(Base):
    __tablename__ = "patrol_drafts"

    draft_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="editing")
    authoring_document: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    draft_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    frozen_sources: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    history_messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    final_human_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    equipment: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    source_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default="standard", server_default="standard"
    )
    curation_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PatrolDeploymentDefinition(Base):
    __tablename__ = "patrol_deployment_definitions"

    definition_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    document_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    compiled_plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    sources: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PatrolAgent(Base):
    __tablename__ = "patrol_agents"

    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checkpoint_ns: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    equipment: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default="standard", server_default="standard"
    )
    curation_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PatrolContextBinding(Base):
    __tablename__ = "patrol_context_bindings"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_patrol_context_binding_agent"),
        UniqueConstraint(
            "managed_context_id", name="uq_patrol_context_binding_managed_context"
        ),
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
        String(32),
        ForeignKey("patrol_agents.agent_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    root_context_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    managed_context_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    control_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="following"
    )
    health_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="idle"
    )
    observed_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    desired_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prepared_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PatrolContextRevision(Base):
    __tablename__ = "patrol_context_revisions"
    __table_args__ = (
        UniqueConstraint(
            "binding_id",
            "source_checkpoint_id",
            name="uq_patrol_context_revision_source",
        ),
        CheckConstraint(
            "status IN ('observed', 'preparing', 'ready', 'publishing', 'superseded', "
            "'approval_required', 'published', 'unchanged', 'error', 'interrupted')",
            name="ck_patrol_context_revision_status",
        ),
    )

    revision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    binding_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("patrol_context_bindings.binding_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_checkpoint_id: Mapped[str] = mapped_column(Text, nullable=False)
    base_binding_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="observed")
    source_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    source_projection_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    authored_messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    execution_messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    disposition_manifest: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    repair_manifest: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    definition_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    projection_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    projection_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    published_context_checkpoint_id: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    prepared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PatrolContextAttempt(Base):
    __tablename__ = "patrol_context_attempts"
    __table_args__ = (
        UniqueConstraint(
            "revision_id", "attempt_number", name="uq_patrol_context_attempt_number"
        ),
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
        String(32),
        ForeignKey("patrol_context_revisions.revision_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    output_method: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    raw_response: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    parsed_response: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DesktopRun(Base):
    __tablename__ = "desktop_runs"
    __table_args__ = (
        Index(
            "uq_desktop_active_main_execution",
            "task_id",
            "execution_thread_id",
            "checkpoint_ns",
            unique=True,
            postgresql_where=text(
                "kind = 'main' AND status IN ('pending', 'running') "
                "AND execution_thread_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_desktop_run_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    deployment_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    origin: Mapped[str] = mapped_column(
        String(32), nullable=False, default="direct_user", server_default="direct_user"
    )
    execution_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    checkpoint_ns: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    context_revision_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("desktop_context_revisions.revision_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    context_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    directive_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    user_intent_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    loop_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    round_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    parent_run_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("desktop_runs.run_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    action_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    equipment: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    workspace_anchor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    input_messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    prompt_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    prompt_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    prompt_cache_hit_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    final_checkpoint_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DesktopMaterial(Base):
    __tablename__ = "desktop_materials"
    __table_args__ = (
        UniqueConstraint("task_id", "relative_path", name="uq_desktop_material_path"),
        UniqueConstraint(
            "workspace_id", "relative_path", name="uq_workspace_material_path"
        ),
        UniqueConstraint("intake_upload_key", name="uq_material_intake_upload"),
        CheckConstraint(
            "(task_id IS NULL) <> (workspace_id IS NULL)",
            name="ck_material_exclusive_owner",
        ),
        CheckConstraint(
            "intake_upload_key IS NULL OR workspace_id IS NOT NULL",
            name="ck_material_intake_owner",
        ),
    )

    material_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("desktop_workspaces.workspace_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    intake_upload_key: Mapped[str | None] = mapped_column(String(65), nullable=True)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    reading_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="full"
    )
    instruction_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="reference"
    )
    retention: Mapped[str] = mapped_column(
        String(20), nullable=False, default="removable"
    )
    digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    git_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RunMaterialBinding(Base):
    __tablename__ = "desktop_run_material_bindings"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "material_id_snapshot", name="uq_run_material_binding_material"
        ),
        UniqueConstraint("run_id", "ordinal", name="uq_run_material_binding_ordinal"),
        CheckConstraint("ordinal >= 0", name="ck_run_material_binding_ordinal"),
    )

    binding_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    material_id_snapshot: Mapped[str] = mapped_column(String(32), nullable=False)
    relative_path_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    digest_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    material_kind_snapshot: Mapped[str] = mapped_column(String(24), nullable=False)
    note: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    must_view_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MaterialGroup(Base):
    __tablename__ = "desktop_material_groups"
    __table_args__ = (
        UniqueConstraint("task_id", "name", name="uq_desktop_material_group_name"),
        CheckConstraint("position >= 0", name="ck_desktop_material_group_position"),
    )

    group_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MaterialGroupMembership(Base):
    __tablename__ = "desktop_material_group_memberships"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_material_group_membership_position"),
    )

    material_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_materials.material_id", ondelete="CASCADE"),
        primary_key=True,
    )
    group_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_material_groups.group_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MaterialVersion(Base):
    __tablename__ = "material_versions"

    version_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    material_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_materials.material_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    commit_id: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SwarmAgent(Base):
    __tablename__ = "swarm_agents"

    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    permissions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    access_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="workspace-write"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    message_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_agent: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    to_agent: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="message")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentMessageDelivery(Base):
    __tablename__ = "agent_message_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "execution_thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_inbox_checkpoint_delivery",
        ),
    )

    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("agent_messages.message_id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(Text, nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelAttemptAudit(Base):
    __tablename__ = "model_attempt_audits"

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(Text, nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    audit: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    usage_accounting: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ToolExecutionAttempt(Base):
    __tablename__ = "tool_execution_attempts"

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    call_id: Mapped[str] = mapped_column(Text, nullable=False)
    call_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentBoardTask(Base):
    __tablename__ = "agent_tasks"

    board_task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    thread_task_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("desktop_threads.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    requirements: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Memory(Base):
    __tablename__ = "memory_items"
    __table_args__ = (
        Index(
            "uq_strategy_package_scope",
            text("(strategy_package ->> 'id')"),
            text("COALESCE(workspace_id, '')"),
            unique=True,
            postgresql_where=text(
                "category = 'evolution_strategy' AND strategy_package IS NOT NULL"
            ),
        ),
    )

    memory_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(
        String(24), nullable=False, default="memory", server_default="memory"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("desktop_workspaces.workspace_id", ondelete="CASCADE"),
        nullable=True,
    )
    strategy: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    strategy_package: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    content_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="complete"
    )

    segments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    source_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, default="manual"
    )
    source_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
