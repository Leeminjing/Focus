r"""本文件对外提供 CompressionAuthorityCommitter 的 Kernel 内原子授权端口。

输入为已锁定 Loop/grant/round/decision/action、closed compression action 与当前数据库事实；输出为
唯一 committed resolution。具体工作流为按固定锁序读取 pending/candidate/current revision，交给纯
CompressionAuthorityPolicy 判定，允许时同事务接受候选、提交 resolution 和 outbox，不触碰 graph。
示例：`resolution = await committer.commit(session, loop, round_row, grant, decision, action, proposal)`。
"""

from __future__ import annotations

from backend.app.desktop.agent_loop.compression_authority.contracts import AutonomousCompressionPolicy
from backend.app.desktop.agent_loop.compression_authority.models import LoopCompressionCandidate
from backend.app.desktop.agent_loop.compression_authority.policy import CompressionAuthorityFacts, CompressionAuthorityPolicy
from backend.app.desktop.agent_loop.compression_authority.repository import CompressionAuthorityRepository
from backend.app.desktop.agent_loop.models import LoopPendingDecision
from backend.app.desktop.context_evolution import ContextRevisionRepository
from backend.app.desktop.models import DesktopThread


class CompressionCommitRejected(RuntimeError):
    pass


class CompressionAuthorityCommitter:
    def __init__(self) -> None:
        self._policy = CompressionAuthorityPolicy()
        self._repository = CompressionAuthorityRepository()
        self._revisions = ContextRevisionRepository()

    async def commit(self, session, loop, round_row, grant, decision, action_row, proposal, idempotency_key: str):
        pending = await session.get(LoopPendingDecision, proposal.pending_decision_id, with_for_update=True)
        candidate = await session.get(LoopCompressionCandidate, proposal.candidate_id, with_for_update=True)
        context = await session.get(DesktopThread, proposal.context_id)
        current = await self._revisions.current(session, proposal.context_id)
        if pending is None or candidate is None or context is None or current is None:
            raise CompressionCommitRejected("compression_authority_fact_missing")
        if context.workspace_id != loop.workspace_id:
            raise CompressionCommitRejected("workspace_mismatch")
        if any(
            not isinstance(item.get("source_hash"), str)
            or not isinstance(item.get("replacement_hash"), str)
            for item in (candidate.normalized_ranges or ())
        ):
            raise CompressionCommitRejected("candidate_evidence_missing")
        policy = AutonomousCompressionPolicy.model_validate(grant.compression_policy)
        facts = CompressionAuthorityFacts(
            loop_status=loop.status,
            loop_id=loop.loop_id,
            workspace_id=loop.workspace_id,
            authority_revision=loop.authority_revision,
            goal_revision=loop.goal_revision,
            round_id=round_row.round_id,
            round_authority_revision=round_row.authority_revision,
            round_goal_revision=round_row.goal_revision,
            grant_loop_id=grant.loop_id,
            grant_revision=grant.revision,
            grant_status=grant.status,
            grant_capabilities=frozenset(grant.capabilities or ()),
            grant_gates=frozenset(grant.delegable_gates or ()),
            grant_context_scope=frozenset(grant.context_scope or ()),
            pending_loop_id=pending.loop_id,
            pending_kind=pending.kind,
            pending_status=pending.status,
            pending_delegable=pending.delegable,
            candidate_loop_id=candidate.loop_id,
            candidate_pending_decision_id=candidate.pending_decision_id,
            candidate_round_id=candidate.round_id,
            candidate_context_id=candidate.context_id,
            candidate_revision_id=candidate.base_context_revision_id,
            candidate_checkpoint_id=candidate.base_checkpoint_id,
            candidate_frontier_hash=candidate.frontier_hash,
            candidate_authority_revision=candidate.authority_revision,
            candidate_goal_revision=candidate.goal_revision,
            candidate_policy_revision=candidate.policy_revision,
            candidate_status=candidate.status,
            candidate_expires_at=candidate.expires_at,
            candidate_before_tokens=candidate.before_tokens,
            candidate_after_tokens=candidate.after_tokens,
            candidate_protection_violations=sum(
                1 for item in (candidate.protection_evidence or ()) if item.get("overlap") is True
            ),
            action_pending_decision_id=proposal.pending_decision_id,
            action_candidate_id=proposal.candidate_id,
            action_context_id=proposal.context_id,
            action_revision_id=proposal.context_revision_id,
            action_checkpoint_id=proposal.checkpoint_id,
            candidate_id=candidate.candidate_id,
            current_revision_id=current.ref.revision_id,
            current_checkpoint_id=current.ref.checkpoint_id,
            current_frontier_hash=round_row.frontier_hash,
            policy_version=policy.version,
            policy_min_reduction_tokens=policy.min_reduction_tokens,
        )
        verdict = self._policy.evaluate(facts)
        if not verdict.allowed:
            if verdict.reason in {"candidate_expired", "authority_revision_changed", "goal_revision_changed", "context_revision_changed", "checkpoint_changed", "frontier_changed"}:
                candidate.status = "expired" if verdict.reason == "candidate_expired" else "superseded"
            raise CompressionCommitRejected(verdict.reason)
        return await self._repository.commit_resolution(
            session,
            loop_id=loop.loop_id,
            round_id=round_row.round_id,
            grant_id=grant.grant_id,
            grant_revision=grant.revision,
            goal_revision=loop.goal_revision,
            decision_id=decision.decision_id,
            action=action_row,
            candidate=candidate,
            pending=pending,
            idempotency_key=idempotency_key,
        )
