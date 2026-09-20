r"""本文件对外提供 LoopKernel、KernelCommitResult 与 KernelRejected 唯一确定性提交边界。

输入为含 fencing token 的 PatrolDecisionIntent 和当前数据库事实；输出为幂等 committed/superseded/rejected 结果。具体工作流为
稳定锁定 Loop/round/grant，先验证活动 owner，再按 Mission revision、机器边界、权力、frontier、workspace、预算、active Run、gate 顺序校验；普通动作
单事务提交，Context Portfolio 与 workspace adoption 先持久授权意图，再由专用 Kernel port 执行外部准备并
原子收口权威状态；配置异步 publication 时只提交持久授权并交给独立发布队列，拒绝或被取代的决策必须在同一事务内把该 round 收敛为终态（拒绝还会把当前轮所属
Loop 交回用户），提交成功后收口该 round 已观察的用户意图；自主压缩由专用 committer 在同一事务内只提交
resolution、不触碰 graph；Worker 无提交端口。示例：`result = await kernel.commit(intent)`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import uuid
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.authority import AuthorityViolation, DelegatedAuthorityGuard
from backend.app.desktop.agent_loop.budgets import LoopBudgetGuard, configured_provider_count
from backend.app.desktop.agent_loop.completion_policy import CompletionCheckPolicy
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.agent_loop.mission_contract import LegacyMissionAdapter
from backend.app.desktop.agent_loop.mission_authority import MissionAuthorityGuard, MissionAuthorityViolation
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.ownership import KernelFencingRejected, LoopFencingGuard
from backend.app.desktop.agent_loop.models import (
    AgentLoop, CompletionVerification, LoopAction, LoopBudgetUsage, LoopContextMembership, LoopDecision, LoopGoalRevision,
    LoopDelegationGrant, LoopDirective, LoopEventOutbox, LoopPendingDecision,
    LoopObservation, LoopRound, LoopUserIntent, LoopWorkerRequest,
)
from backend.app.desktop.agent_loop.provenance import DelegatedDirectiveFactory
from backend.app.desktop.agent_loop.rounds import UNDECIDED_ROUND_STATUSES, terminate_round
from backend.app.desktop.agent_loop.schemas import CriterionVerification, PatrolDecisionIntent
from backend.app.desktop.agent_loop.compression_authority.commit import CompressionAuthorityCommitter, CompressionCommitRejected
from backend.app.desktop.context_curation.models import CurationLane, CurationProgram, PortfolioLaneCandidate, PortfolioRevision
from backend.app.desktop.context_curation.portfolio_publisher import PortfolioSuperseded
from backend.app.desktop.context_evolution.models import ContextRevision
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceSlot
from backend.app.desktop.agent_loop.wait_requests import LoopWaitRequestFactory, LoopWaitRequestService, open_recovery_wait


@dataclass(frozen=True, slots=True)
class KernelCommitResult:
    decision_id: str
    status: str
    action_ids: tuple[str, ...]
    directive_ids: tuple[str, ...]
    reason: str | None = None


class KernelRejected(RuntimeError):
    pass


class LoopPortfolioPublicationPort(Protocol):
    async def publish(self, decision_id: str): ...


class LoopWorkspaceAdoptionPort(Protocol):
    async def adopt(self, decision_id: str) -> KernelCommitResult: ...


class LoopKernel:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        portfolio_publication: LoopPortfolioPublicationPort | None = None,
        workspace_adoption: LoopWorkspaceAdoptionPort | None = None,
        *,
        queue_portfolio_publication: bool = False,
        require_fencing: bool = False,
    ) -> None:
        self._sessions = sessions
        self._portfolio_publication = portfolio_publication
        self._workspace_adoption = workspace_adoption
        self._queue_portfolio_publication = queue_portfolio_publication
        self._require_fencing = require_fencing
        self._fencing = LoopFencingGuard()
        self._authority = DelegatedAuthorityGuard()
        self._mission_authority = MissionAuthorityGuard()
        self._directives = DelegatedDirectiveFactory()
        self._directive_lifecycle = DirectiveLifecycleRepository()
        self._journal = LoopEventJournal()
        self._interventions = InterventionLifecycleRepository()
        self._compression = CompressionAuthorityCommitter()

    async def commit(self, intent: PatrolDecisionIntent) -> KernelCommitResult:
        deferred_id: str | None = None
        deferred_kind: str | None = None
        async with self._sessions.begin() as session:
            existing = await session.scalar(select(LoopDecision).where(LoopDecision.idempotency_key == intent.idempotency_key))
            if existing is not None:
                if existing.status not in {"publishing", "adopting"}:
                    return await self._result(session, existing)
                deferred_id = existing.decision_id
                deferred_kind = existing.status
                if self._require_fencing:
                    await self._fencing.validate_active(session, intent.round_id, intent.fencing_token)
            if deferred_id is None:
                if self._require_fencing:
                    await self._fencing.validate_active(session, intent.round_id, intent.fencing_token)
                deferred_id, deferred_kind, immediate = await self._commit_new(session, intent)
                if immediate is not None:
                    return immediate
        if deferred_kind == "adopting":
            if self._workspace_adoption is None:
                return await self._fail_deferred(deferred_id, "Kernel 未配置 Workspace adoption port")
            try:
                result = await self._workspace_adoption.adopt(deferred_id)
                if result.status == "committed":
                    await self._address_deferred_user_intents(deferred_id)
                return result
            except Exception as exc:
                return await self._fail_deferred(deferred_id, str(exc))
        if deferred_kind == "publishing" and self._queue_portfolio_publication:
            async with self._sessions() as session:
                decision = await session.get(LoopDecision, deferred_id)
                return await self._result(session, decision)
        if self._portfolio_publication is None:
            return await self._fail_deferred(deferred_id, "Kernel 未配置原子 Portfolio publication port")
        try:
            published = await self._portfolio_publication.publish(deferred_id)
        except PortfolioSuperseded as exc:
            return await self._fail_deferred(deferred_id, str(exc), superseded=True)
        except Exception as exc:
            return await self._fail_deferred(deferred_id, str(exc))
        await self._address_deferred_user_intents(deferred_id)
        async with self._sessions() as session:
            decision = await session.get(LoopDecision, deferred_id)
            result = await self._result(session, decision)
            return KernelCommitResult(
                result.decision_id,
                result.status,
                result.action_ids,
                published.directive_ids,
                result.reason,
            )

    async def _commit_new(
        self,
        session: AsyncSession,
        intent: PatrolDecisionIntent,
    ) -> tuple[str | None, str | None, KernelCommitResult | None]:
        loop = await session.scalar(select(AgentLoop).where(AgentLoop.loop_id == intent.loop_id).with_for_update())
        round_row = await session.scalar(select(LoopRound).where(LoopRound.round_id == intent.round_id).with_for_update())
        grant = await session.scalar(select(LoopDelegationGrant).where(LoopDelegationGrant.grant_id == intent.grant_id).with_for_update())
        if loop is None or round_row is None or round_row.loop_id != intent.loop_id:
            raise KernelRejected("Loop 或 round 不存在")
        existing = await session.scalar(
            select(LoopDecision).where(LoopDecision.idempotency_key == intent.idempotency_key)
        )
        if existing is not None:
            return None, None, await self._result(session, existing)
        if round_row.decision_id is not None:
            return None, None, KernelCommitResult(
                intent.decision_id,
                "superseded",
                (),
                (),
                "round_already_decided",
            )
        stale = self._stale_reason(loop, round_row, intent)
        if stale is None:
            stale = await self._observation_stale_reason(session, loop, round_row, intent)
        if stale is not None:
            decision = self._decision(intent, "superseded", {"reason": stale})
            session.add(decision)
            await terminate_round(session, loop, round_row, category="superseded", reason=stale, decision_id=decision.decision_id, wait_for_user=False, allowed_statuses=UNDECIDED_ROUND_STATUSES)
            return None, None, KernelCommitResult(intent.decision_id, "superseded", (), (), stale)
        try:
            await self._mission_authority.validate(session, loop, round_row, intent)
            self._authority.validate(loop, grant, intent)
            await self._validate_runtime(session, loop, round_row, grant, intent)
        except (AuthorityViolation, MissionAuthorityViolation, KernelRejected) as exc:
            decision = self._decision(intent, "rejected", {"reason": str(exc)})
            session.add(decision)
            await session.flush()
            action_ids, directive_ids = await self._record_rejected_actions(session, loop, round_row, grant, decision, intent, str(exc))
            await terminate_round(session, loop, round_row, category="rejected", reason=self._rejection_reason(intent, exc), decision_id=decision.decision_id, allowed_statuses=UNDECIDED_ROUND_STATUSES)
            return None, None, KernelCommitResult(intent.decision_id, "rejected", tuple(action_ids), tuple(directive_ids), str(exc))
        deferred_status = "publishing" if self._has_lane_mutation(intent) else "adopting" if self._has_adoption(intent) else None
        if deferred_status is not None:
            decision = self._decision(intent, deferred_status, {})
            if deferred_status == "publishing" and self._queue_portfolio_publication:
                decision.queued_reason = "awaiting_publication_component"
            session.add(decision)
            await session.flush()
            action_ids = self._authorize_actions(session, loop, decision, intent)
            round_row.decision_id = decision.decision_id
            round_row.status = deferred_status
            loop.health = deferred_status
            loop.revision += 1
            await self._event(
                session,
                loop.loop_id,
                "LoopDecisionAuthorized",
                {"decision_id": decision.decision_id, "round_id": round_row.round_id, "action_ids": action_ids},
                f"decision-authorized:{decision.decision_id}",
            )
            return decision.decision_id, deferred_status, None
        decision = self._decision(intent, "committed", {})
        session.add(decision)
        await session.flush()
        try:
            async with session.begin_nested():
                action_ids, directive_ids = await self._apply_actions(session, loop, round_row, grant, decision, intent)
        except (KernelRejected, ValueError, LookupError) as exc:
            decision.status = "rejected"
            decision.rejection = {"reason": str(exc)}
            action_ids, directive_ids = await self._record_rejected_actions(session, loop, round_row, grant, decision, intent, str(exc))
            await terminate_round(
                session,
                loop,
                round_row,
                category="rejected",
                reason=self._rejection_reason(intent, exc),
                decision_id=decision.decision_id,
                allowed_statuses=UNDECIDED_ROUND_STATUSES,
            )
            return None, None, KernelCommitResult(decision.decision_id, "rejected", tuple(action_ids), tuple(directive_ids), str(exc))
        round_row.decision_id = decision.decision_id
        round_row.status = self._round_status(intent)
        loop.health = self._health(intent)
        loop.revision += 1
        await self._address_user_intents(session, round_row.round_id)
        await self._event(session, loop.loop_id, "LoopDecisionCommitted", {"decision_id": decision.decision_id, "round_id": round_row.round_id}, f"decision:{decision.decision_id}")
        return None, None, KernelCommitResult(decision.decision_id, "committed", tuple(action_ids), tuple(directive_ids))

    async def _address_user_intents(self, session: AsyncSession, round_id: str) -> None:
        intents = tuple(
            (
                await session.scalars(
                    select(LoopUserIntent)
                    .where(LoopUserIntent.observed_round_id == round_id, LoopUserIntent.status == "observed")
                    .with_for_update()
                )
            ).all()
        )
        for intent in intents:
            intent.status = "addressed"
            if intent.delivery_state == "observed":
                await self._interventions.transition(session, intent.intent_id, "addressed")

    async def _address_deferred_user_intents(self, decision_id: str) -> None:
        async with self._sessions.begin() as session:
            round_id = await session.scalar(
                select(LoopDecision.round_id).where(LoopDecision.decision_id == decision_id)
            )
            if round_id:
                await self._address_user_intents(session, round_id)

    @staticmethod
    def _stale_reason(loop: AgentLoop | None, round_row: LoopRound | None, intent: PatrolDecisionIntent) -> str | None:
        if round_row.goal_revision != loop.goal_revision or intent.goal_revision != loop.goal_revision:
            return "mission_revision_changed"
        if loop.revision != intent.loop_revision:
            return "loop_revision_changed"
        if round_row.decision_id is not None:
            return "round_already_decided"
        if round_row.frontier_hash != intent.observed_frontier_hash:
            return "frontier_changed"
        if round_row.workspace_revision != intent.observed_workspace_revision:
            return "workspace_revision_changed"
        return None

    @staticmethod
    async def _observation_stale_reason(
        session: AsyncSession,
        loop: AgentLoop,
        round_row: LoopRound,
        intent: PatrolDecisionIntent,
    ) -> str | None:
        observation = await session.scalar(select(LoopObservation).where(LoopObservation.round_id == round_row.round_id))
        if observation is None:
            return "observation_missing" if intent.observed_projection_sequence else None
        if intent.observed_projection_sequence != observation.projection_sequence:
            return "observation_sequence_changed"
        if intent.base_entity_revisions != observation.base_entity_revisions:
            return "observation_base_revisions_changed"
        current = {
            "loop": loop.revision,
            "mission": loop.goal_revision,
            "grant": loop.authority_revision,
            "workspace": round_row.workspace_revision,
        }
        for key, revision in current.items():
            observed = intent.base_entity_revisions.get(key)
            if observed is not None and observed != revision:
                return f"{key}_base_revision_changed"
        return None

    async def _validate_runtime(self, session, loop, round_row, grant, intent) -> None:
        usage = await session.get(LoopBudgetUsage, loop.loop_id)
        budgets = grant.budgets
        usage_values = {
            field: int(getattr(usage, field, 0) or 0)
            for field in ("rounds", "model_calls", "input_tokens", "output_tokens", "retries", "lanes", "no_progress_count")
        }
        created_at = loop.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        usage_values["duration_seconds"] = max(0, int((datetime.now(UTC) - created_at).total_seconds()))
        usage_values["providers"] = configured_provider_count(loop.equipment or {})
        usage_values["contexts"] = int(
            await session.scalar(
                select(func.count()).select_from(LoopContextMembership).where(
                    LoopContextMembership.loop_id == loop.loop_id,
                    LoopContextMembership.status != "discarded",
                )
            ) or 0
        )
        for action in intent.actions:
            budget = LoopBudgetGuard().evaluate(usage_values, budgets, action.action)
            if budget.status == "exhausted":
                raise KernelRejected("Loop hard budget 已耗尽: " + ",".join(budget.reasons))
            if budget.status == "change_direction":
                raise KernelRejected("连续无进展，禁止继续相同方向")
        if usage is not None and usage.rounds >= int(budgets.get("max_rounds", 50)):
            raise KernelRejected("round budget 已耗尽")
        new_lanes = sum(action.action in {"create_lane", "merge_contexts"} for action in intent.actions)
        if new_lanes > int(budgets.get("max_new_lanes_per_round", 3)):
            raise KernelRejected("本轮新 Lane 超出预算")
        active_lanes = await session.scalar(
            select(func.count()).select_from(CurationLane).where(
                CurationLane.program_id == loop.program_id,
                CurationLane.lifecycle != "retired",
            )
        )
        if int(active_lanes or 0) + new_lanes > int(budgets.get("max_lanes", 8)):
            raise KernelRejected("活动 Lane 总数超出预算")
        if usage_values["contexts"] + new_lanes > int(budgets.get("max_contexts", 16)):
            raise KernelRejected("Loop Context 总数超出预算")
        active = await session.scalar(select(func.count()).select_from(DesktopRun).where(DesktopRun.loop_id == loop.loop_id, DesktopRun.status.in_(["pending", "running"])))
        direct_targets = [
            action.context_id
            for action in intent.actions
            if action.action == "continue_context"
        ]
        if len(direct_targets) != len(set(direct_targets)):
            raise KernelRejected("同一 round 不得向同一 Context 派发多个并行 Run")
        if active and any(action.action in {"continue_context", "create_lane", "update_lane", "merge_contexts", "pause_lane"} for action in intent.actions):
            raise KernelRejected("Loop 已有活动 Run")
        if active and self._has_adoption(intent):
            raise KernelRejected("仍有活动 Run，不能采用隔离 workspace 结果")
        if active and any(action.action == "stop_loop" for action in intent.actions):
            raise KernelRejected("仍有活动 Run，Patrol 必须先等待本轮安全收敛再停止 Loop")
        human_gate = await session.scalar(select(LoopPendingDecision.pending_decision_id).where(LoopPendingDecision.loop_id == loop.loop_id, LoopPendingDecision.status == "pending", LoopPendingDecision.delegable.is_(False)).limit(1))
        if human_gate and not all(action.action == "wait_for_user" for action in intent.actions):
            raise KernelRejected("存在不可委托人工 gate")
        if self._has_lane_mutation(intent):
            allowed = {"continue_context", "create_lane", "update_lane", "merge_contexts", "pause_lane", "discard_membership"}
            if any(action.action not in allowed for action in intent.actions):
                raise KernelRejected("Portfolio mutation 不得与 Worker、完成或终止 action 混合提交")
            await self._validate_lane_sources(session, loop, grant, intent)
        if self._has_adoption(intent):
            if len(intent.actions) != 1:
                raise KernelRejected("workspace adoption 必须独立提交")
            action = intent.actions[0]
            source = await session.get(WorkspaceSlot, action.source_slot_id)
            if (
                source is None
                or source.kind != "isolated"
                or source.owner_loop_id != loop.loop_id
                or source.revision != action.source_revision
            ):
                raise KernelRejected("待采用 workspace slot 不属于当前 Loop 或 revision 已变化")
            if "adopt_workspace_result" not in set(grant.capabilities or []):
                raise KernelRejected("delegation 未授权 workspace result adoption")
        if any(action.action == "apply_context_compression" for action in intent.actions) and len(intent.actions) != 1:
            raise KernelRejected("autonomous compression 必须独立提交")
        for action in intent.actions:
            if action.action == "request_completion":
                await self._validate_completion(session, loop, round_row, action, active, human_gate)

    @staticmethod
    def _has_lane_mutation(intent: PatrolDecisionIntent) -> bool:
        return any(action.action in {"create_lane", "update_lane", "merge_contexts", "pause_lane"} for action in intent.actions)

    @staticmethod
    def _has_adoption(intent: PatrolDecisionIntent) -> bool:
        return any(action.action == "adopt_workspace_result" for action in intent.actions)

    @staticmethod
    async def _validate_lane_sources(session, loop, grant, intent) -> None:
        scope = set(grant.context_scope)
        for action in intent.actions:
            if action.action not in {"create_lane", "update_lane", "merge_contexts"}:
                continue
            if action.action == "merge_contexts" and len(action.plan.source_frontier) < 2:
                raise KernelRejected("merge_contexts 至少需要两个精确来源 revision")
            for ref in action.plan.source_frontier:
                revision = await session.get(ContextRevision, ref.revision_id)
                context = await session.get(DesktopThread, ref.context_id)
                if (
                    revision is None
                    or context is None
                    or revision.context_id != ref.context_id
                    or revision.checkpoint_id != ref.checkpoint_id
                    or context.workspace_id != loop.workspace_id
                ):
                    raise KernelRejected("Lane plan 引用了无效或跨 workspace 的 revision")
                if ref.context_id not in scope:
                    raise KernelRejected("Lane source Context 不在 delegation scope")

    @staticmethod
    def _authorize_actions(session, loop, decision, intent) -> list[str]:
        action_ids: list[str] = []
        for position, intent_action in enumerate(intent.actions):
            action_id = uuid.uuid4().hex
            session.add(
                LoopAction(
                    action_id=action_id,
                    decision_id=decision.decision_id,
                    loop_id=loop.loop_id,
                    position=position,
                    action_type=intent_action.action,
                    payload=intent_action.model_dump(mode="json"),
                    status="authorized",
                )
            )
            action_ids.append(action_id)
        return action_ids

    async def _fail_deferred(
        self,
        decision_id: str,
        reason: str,
        *,
        superseded: bool = False,
    ) -> KernelCommitResult:
        async with self._sessions.begin() as session:
            decision = await session.get(LoopDecision, decision_id, with_for_update=True)
            if decision is None:
                raise KernelRejected("待恢复的 Portfolio decision 不存在")
            if decision.status == "committed":
                return await self._result(session, decision)
            status = "superseded" if superseded else "rejected"
            decision.status = status
            decision.rejection = {"reason": reason}
            actions = list(
                (
                    await session.scalars(
                        select(LoopAction).where(LoopAction.decision_id == decision_id).with_for_update()
                    )
                ).all()
            )
            for action in actions:
                if action.status == "authorized":
                    action.status = status
            round_row = await session.get(LoopRound, decision.round_id, with_for_update=True)
            loop = await session.get(AgentLoop, decision.loop_id, with_for_update=True)
            if round_row is not None and round_row.status in {"publishing", "adopting"}:
                round_row.status = "superseded" if superseded else "error"
            if loop is not None and loop.status == "running" and not superseded:
                loop.health = "degraded"
                await open_recovery_wait(
                    session,
                    loop,
                    f"Loop deferred commit 失败: {reason[:1000]}",
                    source="kernel-deferred-commit",
                    round_id=decision.round_id,
                    scope={"decision_id": decision_id},
                )
            return KernelCommitResult(decision_id, status, tuple(item.action_id for item in actions), (), reason)

    async def _apply_actions(self, session, loop, round_row, grant, decision, intent):
        action_ids: list[str] = []
        directive_ids: list[str] = []
        for position, intent_action in enumerate(intent.actions):
            action_id = uuid.uuid4().hex
            action = LoopAction(action_id=action_id, decision_id=decision.decision_id, loop_id=loop.loop_id, position=position, action_type=intent_action.action, payload=intent_action.model_dump(mode="json"), status="committed")
            session.add(action)
            action_ids.append(action_id)
            if intent_action.action == "continue_context":
                directive, provenance = self._directives.create(
                    loop_id=loop.loop_id, round_id=round_row.round_id, decision_id=decision.decision_id,
                    action_id=action_id, context_id=intent_action.context_id,
                    context_revision_id=intent_action.context_revision_id, content=intent_action.message,
                    actor_id=intent.holder_id, grant_id=grant.grant_id, grant_revision=grant.revision,
                    goal_revision=loop.goal_revision, idempotency_key=f"{intent.idempotency_key}:directive:{position}",
                )
                session.add_all([directive, provenance])
                await session.flush()
                await self._directive_lifecycle.register(session, directive)
                await self._directive_lifecycle.transition(session, directive.directive_id, "authorized")
                directive_ids.append(directive.directive_id)
            elif intent_action.action in {"create_lane", "update_lane", "merge_contexts"}:
                raise KernelRejected("Lane mutation 必须经过原子 Portfolio publication")
            elif intent_action.action == "adopt_workspace_result":
                raise KernelRejected("Workspace adoption 必须经过专用 adoption port")
            elif intent_action.action == "apply_context_compression":
                try:
                    resolution = await self._compression.commit(
                        session,
                        loop,
                        round_row,
                        grant,
                        decision,
                        action,
                        intent_action,
                        f"{intent.idempotency_key}:compression:{position}",
                    )
                except CompressionCommitRejected as exc:
                    raise KernelRejected(str(exc)) from exc
                action.result = {"resolution_id": resolution.resolution_id, "candidate_id": resolution.candidate_id}
            elif intent_action.action == "pause_lane":
                lane = await self._owned_lane(session, loop, intent_action.lane_id)
                lane.lifecycle = "paused"
                lane.publisher_epoch += 1
                memberships = list((await session.scalars(select(LoopContextMembership).where(LoopContextMembership.loop_id == loop.loop_id, LoopContextMembership.lane_id == lane.lane_id, LoopContextMembership.status == "active").with_for_update())).all())
                for membership in memberships:
                    membership.status = "paused"
                action.status = "applied"
            elif intent_action.action == "discard_membership":
                membership = await session.get(LoopContextMembership, intent_action.membership_id, with_for_update=True)
                if membership is None or membership.loop_id != loop.loop_id:
                    raise KernelRejected("待淘汰 Context membership 不属于当前 Loop")
                membership.status = "discarded"
                action.status = "applied"
            elif intent_action.action in {"request_lane_curator", "request_completion_verifier"}:
                session.add(LoopWorkerRequest(worker_request_id=uuid.uuid4().hex, loop_id=loop.loop_id, round_id=round_row.round_id, kind=intent_action.action.removeprefix("request_"), scope=intent_action.model_dump(mode="json")))
            elif intent_action.action == "wait_for_user":
                await LoopWaitRequestService().open(
                    session,
                    loop,
                    LoopWaitRequestFactory.clarification(intent_action.reason, {"decision_id": decision.decision_id}),
                    created_by="portfolio-patrol",
                    correlation_id=decision.decision_id,
                    round_id=round_row.round_id,
                )
            elif intent_action.action == "stop_loop":
                await self._directive_lifecycle.cancel_active(session, loop.loop_id, "patrol_stop_loop")
                loop.status = "stopped"
                loop.health = "idle"
                loop.completed_at = datetime.now(UTC)
                grant.status = "revoked"
                grant.revoked_at = loop.completed_at
            elif intent_action.action == "request_completion":
                loop.status = "completed"
                loop.health = "idle"
                loop.completed_at = datetime.now(UTC)
                loop.final_result = await self._completion_result(session, loop, intent_action)
                grant.status = "revoked"
                grant.revoked_at = loop.completed_at
        return action_ids, directive_ids

    async def _record_rejected_actions(self, session, loop, round_row, grant, decision, intent, reason: str):
        action_ids: list[str] = []
        directive_ids: list[str] = []
        for position, intent_action in enumerate(intent.actions):
            action_id = uuid.uuid5(uuid.NAMESPACE_URL, f"loop-action:{decision.decision_id}:{position}").hex
            session.add(
                LoopAction(
                    action_id=action_id,
                    decision_id=decision.decision_id,
                    loop_id=loop.loop_id,
                    position=position,
                    action_type=intent_action.action,
                    payload=intent_action.model_dump(mode="json"),
                    status="rejected",
                    result={"reason": reason[:2000]},
                )
            )
            action_ids.append(action_id)
            if intent_action.action != "continue_context":
                continue
            directive_id = uuid.uuid5(uuid.NAMESPACE_URL, f"loop-directive:{decision.decision_id}:{position}").hex
            context = await session.get(DesktopThread, intent_action.context_id)
            revision = await session.get(ContextRevision, intent_action.context_revision_id)
            if context is None or revision is None:
                await self._journal.append(
                    session,
                    loop.loop_id,
                    CanonicalEventDraft(
                        kind="directive.rejected",
                        entity_type="directive",
                        entity_id=directive_id,
                        entity_revision=1,
                        correlation_id=round_row.round_id,
                        payload={
                            "directive_id": directive_id,
                            "round_id": round_row.round_id,
                            "decision_id": decision.decision_id,
                            "origin": "patrol",
                            "target_context_id": intent_action.context_id,
                            "state": "rejected",
                            "reason": reason[:2000],
                        },
                        idempotency_key=f"directive:{directive_id}:rejected",
                    ),
                )
                directive_ids.append(directive_id)
                continue
            directive, provenance = self._directives.create(
                loop_id=loop.loop_id,
                round_id=round_row.round_id,
                decision_id=decision.decision_id,
                action_id=action_id,
                context_id=intent_action.context_id,
                context_revision_id=intent_action.context_revision_id,
                content=intent_action.message,
                actor_id=intent.holder_id,
                grant_id=intent.grant_id,
                grant_revision=intent.grant_revision,
                goal_revision=intent.goal_revision,
                idempotency_key=f"{intent.idempotency_key}:directive:{position}",
            )
            directive.directive_id = directive_id
            provenance.directive_id = directive_id
            directive.status = "blocked"
            session.add_all([directive, provenance])
            await session.flush()
            await self._directive_lifecycle.register(session, directive)
            await self._directive_lifecycle.transition(session, directive.directive_id, "rejected", reason=reason)
            directive_ids.append(directive.directive_id)
        return action_ids, directive_ids

    @staticmethod
    async def _completion_result(session, loop, action) -> dict:
        verification = await session.get(CompletionVerification, action.verification_id)
        memberships = list(
            (
                await session.scalars(
                    select(LoopContextMembership)
                    .where(LoopContextMembership.loop_id == loop.loop_id)
                    .order_by(LoopContextMembership.created_at, LoopContextMembership.membership_id)
                )
            ).all()
        )
        directives = list(
            (
                await session.scalars(
                    select(LoopDirective)
                    .where(LoopDirective.loop_id == loop.loop_id)
                    .order_by(LoopDirective.created_at, LoopDirective.directive_id)
                )
            ).all()
        )
        anchors = list(
            (
                await session.scalars(
                    select(RunExecutionAnchor)
                    .join(DesktopRun, DesktopRun.run_id == RunExecutionAnchor.run_id)
                    .where(DesktopRun.loop_id == loop.loop_id)
                    .order_by(RunExecutionAnchor.run_id)
                )
            ).all()
        )
        final_ids = set(action.final_context_ids)
        return {
            "verification": {
                "verification_id": verification.verification_id,
                "conclusion": verification.conclusion,
                "criteria": verification.criteria,
                "unresolved": verification.unresolved,
            },
            "portfolio_revision_id": loop.current_portfolio_revision_id,
            "final_path": [
                {
                    "membership_id": item.membership_id,
                    "context_id": item.context_id,
                    "lane_id": item.lane_id,
                    "status": item.status,
                }
                for item in memberships
                if item.context_id in final_ids
            ],
            "unadopted_lanes": [
                {
                    "membership_id": item.membership_id,
                    "context_id": item.context_id,
                    "lane_id": item.lane_id,
                    "status": item.status,
                }
                for item in memberships
                if item.context_id not in final_ids
            ],
            "workspace": {
                "authoritative_slot_id": action.final_slot_id,
                "run_adoptions": [
                    {
                        "run_id": item.run_id,
                        "slot_id": item.slot_id,
                        "adoption_state": item.adoption_state,
                        "resulting_workspace_revision": item.resulting_workspace_revision,
                    }
                    for item in anchors
                ],
            },
            "delegated_directives": [
                {
                    "directive_id": item.directive_id,
                    "message_id": item.message_id,
                    "context_id": item.target_context_id,
                    "context_revision_id": item.target_context_revision_id,
                    "content": item.content,
                    "status": item.status,
                    "launched_run_id": item.launched_run_id,
                }
                for item in directives
            ],
        }

    @staticmethod
    async def _owned_lane(session, loop, lane_id: str) -> CurationLane:
        lane = await session.get(CurationLane, lane_id, with_for_update=True)
        if lane is None or lane.program_id != loop.program_id:
            raise KernelRejected("Lane 不属于当前 Loop Portfolio")
        return lane


    @staticmethod
    async def _validate_completion(session, loop, round_row, action, active, human_gate) -> None:
        verification = await session.get(CompletionVerification, action.verification_id)
        if verification is None or verification.loop_id != loop.loop_id:
            raise KernelRejected("缺少独立 Completion Verifier 证据")
        if verification.goal_revision != loop.goal_revision or verification.frontier_hash != round_row.frontier_hash or verification.workspace_revision != round_row.workspace_revision:
            raise KernelRejected("Completion verification 已过期")
        if verification.conclusion != "satisfied" or any(item.get("status") != "satisfied" for item in verification.criteria):
            raise KernelRejected("Completion criteria 未全部满足")
        if verification.unresolved:
            raise KernelRejected("Completion verification 仍有 unresolved 项")
        mission = await session.scalar(select(LoopMissionRevision).where(LoopMissionRevision.loop_id == loop.loop_id, LoopMissionRevision.revision == loop.goal_revision))
        goal = None if mission is not None else await session.scalar(select(LoopGoalRevision).where(LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == loop.goal_revision))
        checks = (
            mission.completion_checks
            if mission is not None
            else [item.model_dump(mode="json") for item in LegacyMissionAdapter.convert(goal=goal.goal, task_contract=goal.task_contract, acceptance_criteria=goal.acceptance_criteria).completion_checks]
            if goal is not None
            else []
        )
        criteria = tuple(CriterionVerification.model_validate(item) for item in verification.criteria)
        CompletionCheckPolicy().validate(checks, criteria)
        required = {str(item.get("check_id")) for item in checks if item.get("required", True)}
        satisfied = {item.check_id for item in criteria if item.status == "satisfied"}
        if not required or not required.issubset(satisfied):
            raise KernelRejected("Completion verification 未覆盖全部必需验收条件")
        if active or human_gate:
            raise KernelRejected("仍有活动 Run 或人工 gate")
        pending_decisions = await session.scalar(
            select(func.count()).select_from(LoopPendingDecision).where(
                LoopPendingDecision.loop_id == loop.loop_id,
                LoopPendingDecision.status == "pending",
            )
        )
        if pending_decisions:
            raise KernelRejected("仍有未解决的 pending decision")
        queued = await session.scalar(
            select(func.count()).select_from(LoopDirective).where(
                LoopDirective.loop_id == loop.loop_id,
                LoopDirective.status.in_(["created", "launching", "blocked"]),
            )
        )
        if queued:
            raise KernelRejected("仍有待派发 delegated directive")
        unadopted = await session.scalar(
            select(func.count())
            .select_from(RunExecutionAnchor)
            .join(DesktopRun, DesktopRun.run_id == RunExecutionAnchor.run_id)
            .where(
                DesktopRun.loop_id == loop.loop_id,
                RunExecutionAnchor.adoption_state.in_(["pending", "conflict", "required"]),
            )
        )
        if unadopted:
            raise KernelRejected("仍有未采用或冲突的隔离 workspace 结果")
        program = await session.get(CurationProgram, loop.program_id)
        portfolio = await session.get(PortfolioRevision, loop.current_portfolio_revision_id) if loop.current_portfolio_revision_id else None
        if program is None or portfolio is None or program.current_portfolio_revision_id != portfolio.portfolio_revision_id or portfolio.status != "published":
            raise KernelRejected("最终 Portfolio publication 不完整")
        memberships = set((await session.scalars(select(LoopContextMembership.context_id).where(LoopContextMembership.loop_id == loop.loop_id, LoopContextMembership.status == "active"))).all())
        if not set(action.final_context_ids).issubset(memberships):
            raise KernelRejected("最终 Context 路径不属于活动 Portfolio")
        published_contexts = set(
            (
                await session.scalars(
                    select(PortfolioLaneCandidate.target_context_id).where(
                        PortfolioLaneCandidate.portfolio_revision_id == portfolio.portfolio_revision_id,
                        PortfolioLaneCandidate.status.in_(["prepared", "unchanged", "published"]),
                    )
                )
            ).all()
        )
        if not set(action.final_context_ids).issubset(published_contexts):
            raise KernelRejected("最终 Context 路径未进入当前已发布 Portfolio")
        slot = await session.get(WorkspaceSlot, action.final_slot_id)
        if slot is None or slot.kind != "authoritative" or slot.revision != round_row.workspace_revision:
            raise KernelRejected("最终 workspace 未采用到当前权威 revision")

    @staticmethod
    def _rejection_reason(intent: PatrolDecisionIntent, exc: Exception) -> str:
        if any(action.action == "request_completion" for action in intent.actions):
            return f"Completion Guard 未通过: {str(exc)[:1000]}"
        return str(exc)

    @staticmethod
    def _decision(intent: PatrolDecisionIntent, status: str, rejection: dict) -> LoopDecision:
        return LoopDecision(decision_id=intent.decision_id, loop_id=intent.loop_id, round_id=intent.round_id, holder_id=intent.holder_id, intent=intent.model_dump(mode="json"), rationale=intent.rationale, evidence=list(intent.evidence), status=status, idempotency_key=intent.idempotency_key, rejection=rejection, fencing_token=intent.fencing_token)

    @staticmethod
    def _round_status(intent: PatrolDecisionIntent) -> str:
        if any(action.action == "apply_context_compression" for action in intent.actions):
            return "resolving_gate"
        if any(action.action.startswith("request_") for action in intent.actions):
            return "waiting_workers"
        if any(action.action in {"continue_context", "create_lane", "update_lane", "merge_contexts"} for action in intent.actions):
            return "ready"
        return "settled"

    @staticmethod
    def _health(intent: PatrolDecisionIntent) -> str:
        if any(action.action == "apply_context_compression" for action in intent.actions):
            return "resuming"
        if any(action.action.startswith("request_") for action in intent.actions):
            return "waiting_workers"
        if any(action.action in {"continue_context", "create_lane", "update_lane", "merge_contexts"} for action in intent.actions):
            return "dispatching"
        return "idle"

    @staticmethod
    async def _event(session, loop_id: str, event_type: str, payload: dict, key: str) -> None:
        sequence = await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop_id))
        session.add(LoopEventOutbox(event_id=uuid.uuid4().hex, loop_id=loop_id, sequence=int(sequence or 0) + 1, event_type=event_type, payload=payload, idempotency_key=key))

    @staticmethod
    async def _result(session, decision: LoopDecision) -> KernelCommitResult:
        actions = list((await session.scalars(select(LoopAction).where(LoopAction.decision_id == decision.decision_id).order_by(LoopAction.position))).all())
        directives = list((await session.scalars(select(LoopDirective).where(LoopDirective.decision_id == decision.decision_id))).all())
        return KernelCommitResult(decision.decision_id, decision.status, tuple(item.action_id for item in actions), tuple(item.directive_id for item in directives), decision.rejection.get("reason"))
