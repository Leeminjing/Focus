r"""本文件对外提供无副作用 CompressionAuthorityPolicy 及其 facts/verdict。

输入为 Loop、grant、round、pending decision、candidate 与当前 execution identity 的标量事实；输出为
allowed 或带稳定 reason code 的拒绝 verdict。具体工作流为按授权、版本、所有权、frontier、过期、
保护锚点与减量顺序比较，不访问数据库或 graph。示例：`policy.evaluate(facts).allowed`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class CompressionAuthorityFacts:
    loop_status: str
    loop_id: str
    workspace_id: str
    authority_revision: int
    goal_revision: int
    round_id: str
    round_authority_revision: int
    round_goal_revision: int
    grant_loop_id: str
    grant_revision: int
    grant_status: str
    grant_capabilities: frozenset[str]
    grant_gates: frozenset[str]
    grant_context_scope: frozenset[str]
    pending_loop_id: str
    pending_kind: str
    pending_status: str
    pending_delegable: bool
    candidate_loop_id: str
    candidate_pending_decision_id: str
    candidate_round_id: str
    candidate_context_id: str
    candidate_revision_id: str
    candidate_checkpoint_id: str
    candidate_frontier_hash: str
    candidate_authority_revision: int
    candidate_goal_revision: int
    candidate_policy_revision: int
    candidate_status: str
    candidate_expires_at: datetime
    candidate_before_tokens: int
    candidate_after_tokens: int
    candidate_protection_violations: int
    action_pending_decision_id: str
    action_candidate_id: str
    action_context_id: str
    action_revision_id: str
    action_checkpoint_id: str
    candidate_id: str
    current_revision_id: str
    current_checkpoint_id: str
    current_frontier_hash: str
    policy_version: int
    policy_min_reduction_tokens: int


@dataclass(frozen=True, slots=True)
class CompressionAuthorityVerdict:
    allowed: bool
    reason: str


class CompressionAuthorityPolicy:
    def evaluate(self, facts: CompressionAuthorityFacts) -> CompressionAuthorityVerdict:
        checks = (
            (facts.loop_status == "running", "loop_not_running"),
            (facts.grant_status == "active", "grant_not_active"),
            (facts.grant_loop_id == facts.loop_id == facts.pending_loop_id == facts.candidate_loop_id, "ownership_mismatch"),
            ("compression" in facts.grant_gates, "compression_gate_not_delegated"),
            ("apply_context_compression" in facts.grant_capabilities, "compression_action_not_delegated"),
            (facts.pending_kind == "compression" and facts.pending_status == "pending" and facts.pending_delegable, "pending_not_delegable"),
            (facts.authority_revision == facts.grant_revision == facts.round_authority_revision == facts.candidate_authority_revision, "authority_revision_changed"),
            (facts.goal_revision == facts.round_goal_revision == facts.candidate_goal_revision, "goal_revision_changed"),
            (facts.candidate_round_id == facts.round_id, "round_changed"),
            (facts.candidate_pending_decision_id == facts.action_pending_decision_id, "pending_identity_mismatch"),
            (facts.candidate_id == facts.action_candidate_id, "candidate_identity_mismatch"),
            (facts.candidate_context_id == facts.action_context_id and facts.candidate_context_id in facts.grant_context_scope, "context_scope_mismatch"),
            (facts.candidate_revision_id == facts.action_revision_id == facts.current_revision_id, "context_revision_changed"),
            (facts.candidate_checkpoint_id == facts.action_checkpoint_id == facts.current_checkpoint_id, "checkpoint_changed"),
            (facts.candidate_frontier_hash == facts.current_frontier_hash, "frontier_changed"),
            (facts.candidate_policy_revision == facts.policy_version, "policy_revision_changed"),
            (facts.candidate_status == "prepared", "candidate_not_prepared"),
            (self._aware(facts.candidate_expires_at) > datetime.now(UTC), "candidate_expired"),
            (facts.candidate_protection_violations == 0, "protected_anchor_overlap"),
            (
                facts.candidate_before_tokens - facts.candidate_after_tokens >= facts.policy_min_reduction_tokens,
                "insufficient_token_reduction",
            ),
        )
        for valid, reason in checks:
            if not valid:
                return CompressionAuthorityVerdict(False, reason)
        return CompressionAuthorityVerdict(True, "authorized")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
