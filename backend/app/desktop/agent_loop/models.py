r"""本文件对外提供 Agent Loop、delegation、fencing、round、directive、recovery opportunity、completion 与 audit ORM 实体。

输入为用户目标、版本化授权（含自主压缩 policy）、Context/Workspace frontier、Patrol 判断和 Kernel 结果；输出为可恢复、
可审计且具单 writer 约束的 Loop 状态。具体工作流为 goal/grant 定义权力，round/observation 冻结事实，
decision/action 记录判断与异步发布尝试，recovery opportunity 冻结单来源恢复 authority 并记录原子消费，user intent 保存用户对 Context 或 Portfolio 的外部控制意见，
directive/provenance 以可见排队原因驱动 Run，fencing counter 拒绝旧 owner，completion/outbox 收敛生命周期。
示例：`loop = AgentLoop(loop_id="l1", status="running", ...)`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base


class AgentLoop(Base):
    __tablename__ = "agent_loops"
    __table_args__ = (
        CheckConstraint("status IN ('draft','running','pausing','paused','waiting_user','completing','completed','stopping','stopped','failed')", name="ck_agent_loop_status"),
        CheckConstraint("revision > 0 AND authority_revision > 0 AND goal_revision > 0", name="ck_agent_loop_revisions"),
        Index("uq_agent_loop_active_context", "initial_context_id", unique=True, postgresql_where=text("status IN ('running','pausing','paused','waiting_user','completing')")),
    )

    loop_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_workspaces.workspace_id", ondelete="CASCADE"), nullable=False, index=True)
    initial_context_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"), nullable=False, index=True)
    program_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("curation_programs.program_id", ondelete="SET NULL"), nullable=True)
    holder_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", server_default="draft")
    health: Mapped[str] = mapped_column(String(24), nullable=False, default="idle", server_default="idle")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    authority_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    goal_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    current_round_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_portfolio_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    equipment: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    final_result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    waiting_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoopCoordinatorLease(Base):
    __tablename__ = "loop_coordinator_leases"

    lease_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    round_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    owner_id: Mapped[str] = mapped_column(String(120), nullable=False)
    fencing_token: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LoopCoordinatorFence(Base):
    __tablename__ = "loop_coordinator_fences"

    round_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), primary_key=True
    )
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LoopGoalRevision(Base):
    __tablename__ = "loop_goal_revisions"
    __table_args__ = (UniqueConstraint("loop_id", "revision", name="uq_loop_goal_revision"),)

    goal_revision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    task_contract: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_criteria: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    authored_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoopDelegationGrant(Base):
    __tablename__ = "loop_delegation_grants"
    __table_args__ = (
        UniqueConstraint("loop_id", "revision", name="uq_loop_grant_revision"),
        Index("uq_loop_active_authority_holder", "loop_id", unique=True, postgresql_where=text("status = 'active'")),
    )

    grant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    holder_id: Mapped[str] = mapped_column(String(64), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    context_scope: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    permission_scope: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    budgets: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    delegable_gates: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    compression_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoopContextMembership(Base):
    __tablename__ = "loop_context_memberships"
    __table_args__ = (UniqueConstraint("loop_id", "context_id", name="uq_loop_context_membership"),)

    membership_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    context_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"), nullable=False, index=True)
    lane_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("curation_lanes.lane_id", ondelete="SET NULL"), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    required_barrier: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoopUserIntent(Base):
    __tablename__ = "loop_user_intents"
    __table_args__ = (
        CheckConstraint("scope IN ('context','portfolio')", name="ck_loop_user_intent_scope"),
        CheckConstraint("status IN ('pending','observed','addressed','superseded')", name="ck_loop_user_intent_status"),
        CheckConstraint(
            "(scope = 'context' AND target_context_id IS NOT NULL) OR "
            "(scope = 'portfolio' AND target_context_id IS NULL)",
            name="ck_loop_user_intent_target",
        ),
        Index("ix_loop_user_intent_pending", "loop_id", "status", "created_at"),
    )

    intent_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    target_context_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"), nullable=True, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    delivery_state: Mapped[str] = mapped_column(String(24), nullable=False, default="submitted", server_default="submitted")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    origin_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="user", server_default="user")
    correlation_id: Mapped[str] = mapped_column(String(120), nullable=False)
    resulting_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    goal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    authority_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_round_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    addressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoopInterventionTransition(Base):
    __tablename__ = "loop_intervention_transitions"
    __table_args__ = (UniqueConstraint("intent_id", "revision", name="uq_loop_intervention_transition_revision"),)

    transition_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_user_intents.intent_id", ondelete="CASCADE"), nullable=False, index=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str] = mapped_column(String(24), nullable=False)
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoopRound(Base):
    __tablename__ = "loop_rounds"
    __table_args__ = (UniqueConstraint("loop_id", "number", name="uq_loop_round_number"),)

    round_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="observed", server_default="observed")
    observation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    authority_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    goal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    frontier_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    barrier: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoopObservation(Base):
    __tablename__ = "loop_observations"

    observation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False, unique=True)
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    envelope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    base_entity_revisions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoopContextRecoveryOpportunity(Base):
    __tablename__ = "loop_context_recovery_opportunities"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','consumed','stale','blocked')",
            name="ck_loop_context_recovery_status",
        ),
        UniqueConstraint(
            "loop_id",
            "source_revision_id",
            "source_run_id",
            "compiler_version",
            name="uq_loop_context_recovery_source",
        ),
        Index("ix_loop_context_recovery_round_status", "round_id", "status"),
    )

    opportunity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    loop_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False
    )
    source_context_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"), nullable=False
    )
    source_revision_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("desktop_context_revisions.revision_id", ondelete="RESTRICT"), nullable=False
    )
    source_frontier_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    goal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    authority_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    grant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    grant_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_run_id: Mapped[str] = mapped_column(String(32), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    safe_summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    consumed_by_decision_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("loop_decisions.decision_id", ondelete="SET NULL"), nullable=True
    )
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoopPatrolAttempt(Base):
    __tablename__ = "loop_patrol_attempts"
    __table_args__ = (UniqueConstraint("round_id", "attempt", name="uq_loop_patrol_attempt"),)

    patrol_attempt_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(Text, nullable=False)
    observation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    raw_output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoopDecision(Base):
    __tablename__ = "loop_decisions"
    __table_args__ = (UniqueConstraint("round_id", name="uq_loop_round_decision"), UniqueConstraint("idempotency_key", name="uq_loop_decision_idempotency"))

    decision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False)
    holder_id: Mapped[str] = mapped_column(String(64), nullable=False)
    intent: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    rejection: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    deferred_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    queued_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoopAction(Base):
    __tablename__ = "loop_actions"
    __table_args__ = (UniqueConstraint("decision_id", "position", name="uq_loop_action_position"),)

    action_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_decisions.decision_id", ondelete="CASCADE"), nullable=False, index=True)
    loop_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")


class LoopDirective(Base):
    __tablename__ = "loop_directives"
    __table_args__ = (UniqueConstraint("message_id", name="uq_loop_directive_message"), UniqueConstraint("idempotency_key", name="uq_loop_directive_idempotency"))

    directive_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_decisions.decision_id", ondelete="CASCADE"), nullable=False)
    action_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_actions.action_id", ondelete="CASCADE"), nullable=False)
    target_context_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_threads.task_id", ondelete="RESTRICT"), nullable=False)
    target_context_revision_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_context_revisions.revision_id", ondelete="RESTRICT"), nullable=False)
    message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="patrol", server_default="patrol")
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    grant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    grant_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    goal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="created", server_default="created")
    lifecycle_state: Mapped[str] = mapped_column(String(24), nullable=False, default="proposed", server_default="proposed")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    origin_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="patrol", server_default="patrol")
    correlation_id: Mapped[str] = mapped_column(String(120), nullable=False)
    causation_event_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    launched_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    queued_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoopDirectiveTransition(Base):
    __tablename__ = "loop_directive_transitions"
    __table_args__ = (UniqueConstraint("directive_id", "revision", name="uq_loop_directive_transition_revision"),)

    transition_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    directive_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_directives.directive_id", ondelete="CASCADE"), nullable=False, index=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str] = mapped_column(String(24), nullable=False)
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    caused_by_event_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MessageProvenance(Base):
    __tablename__ = "message_provenance"
    __table_args__ = (UniqueConstraint("context_revision_id", "message_id", name="uq_message_provenance"),)

    provenance_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    context_revision_id: Mapped[str] = mapped_column(String(32), ForeignKey("desktop_context_revisions.revision_id", ondelete="CASCADE"), nullable=False, index=True)
    message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    directive_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("loop_directives.directive_id", ondelete="SET NULL"), nullable=True)
    audit: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")


class LoopBudgetUsage(Base):
    __tablename__ = "loop_budget_usage"

    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), primary_key=True)
    rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    lanes: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    no_progress_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    no_progress_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LoopWorkerRequest(Base):
    __tablename__ = "loop_worker_requests"

    worker_request_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    retry_identity: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CompletionVerification(Base):
    __tablename__ = "loop_completion_verifications"

    verification_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False)
    goal_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    frontier_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    criteria: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    conclusion: Mapped[str] = mapped_column(String(20), nullable=False)
    unresolved: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    worker_request_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoopPendingDecision(Base):
    __tablename__ = "loop_pending_decisions"

    pending_decision_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    delegable: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoopEventOutbox(Base):
    __tablename__ = "loop_event_outbox"
    __table_args__ = (UniqueConstraint("loop_id", "sequence", name="uq_loop_event_sequence"), UniqueConstraint("idempotency_key", name="uq_loop_event_idempotency"))

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
