r"""本文件对外提供 LoopKernel、KernelCommitResult 与 KernelRejected 唯一确定性提交边界。

输入为绑定 Observation identity/generation/hash、fencing token 与完整 Context 权限的 PatrolDecisionIntent；输出为幂等 committed/superseded/rejected 结果。具体工作流为
稳定锁定 Loop/round/grant，先验证活动 owner，再按 Mission revision、机器边界、权力、frontier、workspace、恢复机会、预算、active Run、gate 顺序校验；
澄清动作在同一事务内根据当前事实再次验证 cause 与 evidence identity，只有准入后才写等待请求；
无副作用 decline_expansion 仅形成审计 action，普通动作
单事务提交，Context Portfolio 与 workspace adoption 先持久授权意图，再由专用 Kernel port 执行外部准备并
原子收口权威状态；Mission bootstrap 与 Patrol 共用 continuation 提交边界且只在外部记录来源；配置异步 publication 时只提交持久授权并交给独立发布队列，拒绝或被取代的延迟决策必须在同一事务内复用Round终止入口记录结算和事件，并把仍运行的当前轮所属
Loop交回用户；成功权威凭据与 Round.decision_id 不回退，提交前拒绝只留候选审计与恢复等待，提交成功后收口该 round 已观察的用户意图；自主压缩由专用committer在同一事务内只提交
resolution、不触碰 graph；completed/stopped/failed 委托 TerminalLifecycle 原子收敛并释放策展所有权；执行阶段与控制状态由共享生命周期入口发布，实体版本不代替控制版本；Worker 无提交端口。示例：`result = await kernel.commit(intent)`。
直接用户消息经专职适配器核验冻结身份并生成用户来源 Directive；普通意见收口不会把直接消息标为 addressed，未处理消息阻止完成。
完成动作复用共享只读资格政策并在当前权威事务重读具体验证及来源。
完成 Worker 受理前在当前控制事务复检同源语义输入，等价已验证输入零新 Worker；语义合同与审计来源保存于现有 scope。
延迟失败只先读取决策归属，再依 Loop、Round、Decision 顺序加锁并刷新；已落定的决策原样返回，迟到回调不覆盖暂停终态。
workspace_input 由 apply_user_inputs 完整处置，有来源的 Mission 修订、逐 Context 权限、精确取消与新工作在同一授权事务提交；发布重放只处理尚未生效动作，后续失败不能撤回已提交输入或取消。workspace 完成落点保留绑定并 idle，旧 Context Loop 仍走终态。
Savepoint 回退后显式刷新控制实体。仅迁移前 schema0 且无 DecisionInputs 的 Observation 不登记认知沉淀 work，执行来源仍由全轮汇合吸收。
消息动作的附件选择仅来自本轮冻结观察或其精确输入因果链，提交前核验工作区，Dispatcher 从同一已提交观察解析版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import uuid
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.patrol.agent_loop.governance.authority import AuthorityViolation, DelegatedAuthorityGuard
from backend.app.desktop.patrol.agent_loop.resources.budgets import LoopBudgetGuard, configured_provider_count
from backend.app.desktop.patrol.agent_loop.inputs.clarification_admission import ClarificationAdmissionPolicy, ClarificationFactsReader, ClarificationRejected
from backend.app.desktop.patrol.agent_loop.execution.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.patrol.agent_loop.journal.event_contract import CanonicalEventDraft
from backend.app.desktop.patrol.agent_loop.journal.event_journal import LoopEventJournal
from backend.app.desktop.patrol.agent_loop.governance.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.patrol.agent_loop.inputs.user_message_delivery import LoopUserMessageDelivery, UserMessageDeliveryRejected
from backend.app.desktop.patrol.agent_loop.missions.mission_authority import MissionAuthorityGuard, MissionAuthorityViolation
from backend.app.desktop.patrol.agent_loop.governance.ownership import KernelFencingRejected, LoopFencingGuard
from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop, CompletionVerification, LoopAction, LoopBudgetUsage, LoopContextMembership, LoopDecision,
    LoopDelegationGrant, LoopDirective, LoopEventOutbox, LoopPendingDecision,
    LoopObservation, LoopRound, LoopUserIntent, LoopWorkerRequest,
)
from backend.app.desktop.patrol.agent_loop.governance.provenance import DelegatedDirectiveFactory
from backend.app.desktop.patrol.agent_loop.rounds.lifecycle import UNDECIDED_ROUND_STATUSES, terminate_round
from backend.app.desktop.patrol.agent_loop.schemas import PatrolDecisionIntent
from backend.app.desktop.patrol.agent_loop.compression_authority.commit import CompressionAuthorityCommitter, CompressionCommitRejected
from backend.app.desktop.patrol.agent_loop.context_expansion.repository import ContextExpansionRepository
from backend.app.desktop.patrol.agent_loop.context_expansion.models import LoopContextExpansion
from backend.app.desktop.patrol.agent_loop.context_recovery import (
    ContextRecoveryAuthorityError,
    ContextRecoveryAuthorityValidator,
    ContextRecoveryOpportunityRepository,
)
from backend.app.desktop.contexts.curation.models import CurationLane, PortfolioRevision
from backend.app.desktop.contexts.curation.portfolio_publisher import PortfolioSuperseded
from backend.app.desktop.contexts.evolution.models import ContextRevision
from backend.app.desktop.persistence.models import DesktopRun, DesktopThread
from backend.app.desktop.workspaces.coordination.models import RunExecutionAnchor, WorkspaceSlot
from backend.app.desktop.patrol.agent_loop.waits.wait_requests import LoopWaitRequestFactory, LoopWaitRequestService, open_recovery_wait
from backend.app.desktop.patrol.agent_loop.lifecycle.terminal_lifecycle import LoopTerminalLifecycle


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
        self._user_messages = LoopUserMessageDelivery()
        self._recovery_opportunities = ContextRecoveryOpportunityRepository()
        self._recovery_authority = ContextRecoveryAuthorityValidator()
        self._compression = CompressionAuthorityCommitter()
        self._terminal = LoopTerminalLifecycle()
        self._expansions = ContextExpansionRepository()
        self._clarification_facts = ClarificationFactsReader()
        self._clarification_policy = ClarificationAdmissionPolicy()

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
            if deferred_id is None:
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

    async def _commit_new(self, session, intent):
        loop = await session.get(AgentLoop, intent.loop_id, with_for_update=True, populate_existing=True)
        round_row = await session.get(LoopRound, intent.round_id, with_for_update=True, populate_existing=True)
        grant = await session.get(LoopDelegationGrant, intent.grant_id, with_for_update=True, populate_existing=True)
        if loop is None or round_row is None or round_row.loop_id != intent.loop_id:
            raise KernelRejected("Loop 或 round 不存在")
        existing = await session.scalar(select(LoopDecision).where(LoopDecision.idempotency_key == intent.idempotency_key))
        if existing is not None:
            return None, None, await self._result(session, existing)
        if self._require_fencing:
            await self._fencing.validate_active(session, intent.round_id, intent.fencing_token)
        stale = self._stale_reason(loop, round_row, intent)
        if stale is None:
            stale = await self._observation_stale_reason(session, loop, round_row, intent)
        if stale is not None:
            decision = self._decision(intent, "superseded", {"reason": stale})
            session.add(decision)
            await self._terminate_expansions(session, intent.model_dump(mode="json"), "superseded", stale)
            return None, None, KernelCommitResult(intent.decision_id, "superseded", (), (), stale)
        deferred = "publishing" if self._has_lane_mutation(intent) else "adopting" if self._has_adoption(intent) else None
        decision = self._decision(intent, deferred or "committed", {})
        session.add(decision)
        await session.flush()
        observation = await session.get(LoopObservation, round_row.observation_id) if round_row.observation_id else None
        try:
            async with session.begin_nested():
                self._authority.validate(loop, grant, intent)
                input_results = await self._apply_inputs(session, loop, round_row, intent)
                effective_intent = intent.model_copy(update={"goal_revision": loop.goal_revision})
                await self._mission_authority.validate(session, loop, round_row, effective_intent)
                await self._validate_runtime(session, loop, round_row, grant, effective_intent)
                from backend.app.desktop.patrol.agent_loop.execution.directive_equipment import validate_context_permissions

                if observation is None:
                    raise KernelRejected("决策缺少冻结 Observation")
                from backend.app.desktop.capabilities.evolution_strategies import validate_strategy_adoptions

                validate_strategy_adoptions(intent.strategy_adoptions, observation.envelope.get("evolution_strategies", ()), len(intent.actions))
                from backend.app.desktop.patrol.agent_loop.schemas import LoopObservationEnvelope

                await validate_context_permissions(session, loop, LoopObservationEnvelope.model_validate(observation.envelope), intent)
                if deferred:
                    action_ids = self._authorize_actions(session, loop, decision, intent)
                    directive_ids = []
                    await session.flush()
                    from backend.app.desktop.patrol.agent_loop.execution.execution_cancellation import ExecutionCancellation

                    for position, action in enumerate(intent.actions):
                        row = await session.get(LoopAction, action_ids[position])
                        if action.action == "apply_user_inputs":
                            row.result, row.status = input_results[position], "applied"
                        elif action.action == "cancel_execution":
                            row.result = await ExecutionCancellation().apply(session, loop, observation, action, row.action_id)
                            row.status = "applied"
                    await self._authorize_expansions(session, intent, decision.decision_id)
                else:
                    action_ids, directive_ids = await self._apply_actions(session, loop, round_row, grant, decision, intent, input_results)
                decision.intent = effective_intent.model_dump(mode="json")
        except (AuthorityViolation, MissionAuthorityViolation, KernelRejected, ValueError, LookupError) as exc:
            for entity in (loop, round_row, decision):
                await session.refresh(entity)
            if grant is not None:
                await session.refresh(grant)
            decision.status, decision.rejection = "rejected", {"reason": str(exc)}
            action_ids, directive_ids = await self._record_rejected_actions(session, loop, round_row, grant, decision, intent, str(exc))
            if loop.status == "running":
                await open_recovery_wait(session, loop, self._rejection_reason(intent, exc),
                    source="kernel-rejected", round_id=round_row.round_id,
                    scope={"decision_id": decision.decision_id, "observation_id": round_row.observation_id})
            return None, None, KernelCommitResult(decision.decision_id, "rejected", tuple(action_ids), tuple(directive_ids), str(exc))
        decision.authority_committed_at = datetime.now(UTC)
        decision.observation_id = round_row.observation_id
        decision.observation_generation = observation.generation if observation else intent.observation_generation
        round_row.decision_id = decision.decision_id
        desired_status = deferred or self._round_status(intent)
        round_row.status = "running" if desired_status == "settled" else desired_status
        if deferred == "publishing" and self._queue_portfolio_publication:
            decision.queued_reason = "awaiting_publication_component"
        await session.flush()
        if observation is not None and observation.envelope.get("input_schema_version") == 1:
            from backend.app.desktop.patrol.agent_loop.task_progress.work_queue import TaskProgressQueue

            await TaskProgressQueue().enqueue_observation(session, loop.loop_id, observation.observation_id)
        if desired_status == "settled":
            from backend.app.desktop.patrol.agent_loop.rounds.lifecycle import settle_round

            await settle_round(session, loop, round_row)
        if loop.status == "running":
            loop.health = deferred or self._health(intent)
        loop.revision += 1
        from backend.app.desktop.patrol.agent_loop.journal.lifecycle_events import LoopLifecycleEventRecorder
        from backend.app.desktop.patrol.agent_loop.journal.round_events import RoundStateEventRecorder

        await self._address_user_intents(session, round_row.round_id)
        from backend.app.desktop.patrol.agent_loop.journal.timeline_events import record_work_decision

        await record_work_decision(session, decision)
        await RoundStateEventRecorder().record(session, round_row)
        await LoopLifecycleEventRecorder().record(session, loop)
        await self._event(session, loop.loop_id, "LoopDecisionAuthorized" if deferred else "LoopDecisionCommitted",
            {"decision_id": decision.decision_id, "round_id": round_row.round_id,
             "observation_id": decision.observation_id, "observation_generation": decision.observation_generation,
             "authority_committed_at": decision.authority_committed_at.isoformat(), "action_ids": action_ids},
            f"decision-authorized:{decision.decision_id}" if deferred else f"decision:{decision.decision_id}")
        if deferred:
            return decision.decision_id, deferred, None
        return None, None, KernelCommitResult(decision.decision_id, "committed", tuple(action_ids), tuple(directive_ids))

    @staticmethod
    async def _apply_inputs(session, loop, round_row, intent):
        from backend.app.desktop.patrol.agent_loop.inputs.input_application import UserInputApplication

        applications = [(position, action) for position, action in enumerate(intent.actions) if action.action == "apply_user_inputs"]
        if len(applications) > 1:
            raise KernelRejected("每个决策只能包含一个完整输入处置动作")
        observation = await session.get(LoopObservation, round_row.observation_id) if round_row.observation_id else None
        required = tuple(item for item in (observation.envelope.get("user_intents", ()) if observation else ())
                         if item.get("intent_kind") == "workspace_input")
        if required and not applications:
            raise KernelRejected("决策缺少完整冻结输入处置")
        return {position: await UserInputApplication().apply(session, loop, round_row, action)
                for position, action in applications}

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
            if intent.intent_kind in {"direct_message", "workspace_input"}:
                continue
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
        if round_row.status in {"settled", "error", "superseded"}:
            return "round_terminated"
        if round_row.goal_revision != loop.goal_revision or intent.goal_revision != loop.goal_revision:
            return "mission_revision_changed"
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
        observation = await session.get(LoopObservation, round_row.observation_id) if round_row.observation_id else None
        if observation is None:
            return "observation_missing" if intent.observed_projection_sequence else None
        if intent.observation_id and intent.observation_id != observation.observation_id:
            return "observation_superseded"
        if intent.observation_generation != observation.generation:
            return "observation_generation_changed"
        if intent.observed_envelope_hash and intent.observed_envelope_hash != observation.envelope_hash:
            return "observation_hash_changed"
        if observation.generation > 1 and not intent.observation_id:
            return "observation_identity_missing"
        if intent.observed_projection_sequence != observation.projection_sequence:
            return "observation_sequence_changed"
        if intent.base_entity_revisions != observation.base_entity_revisions:
            return "observation_base_revisions_changed"
        current = {
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
        applications = [action for action in intent.actions if action.action == "apply_user_inputs"]
        if len(applications) > 1:
            raise KernelRejected("输入处置动作必须唯一")
        message_targets = []
        for action in intent.actions:
            attachment_ids = getattr(action, "attachment_input_ids", ())
            if attachment_ids:
                from backend.app.desktop.patrol.agent_loop.workspaces.workspace_attachments import WorkspaceAttachments

                observation = await session.get(LoopObservation, round_row.observation_id)
                if observation is None:
                    raise KernelRejected("附件授权缺少冻结观察")
                try:
                    WorkspaceAttachments().authorized(observation.envelope, attachment_ids, loop.workspace_id)
                except ValueError as error:
                    raise KernelRejected(str(error)) from error
            if action.action == "deliver_user_message":
                try:
                    message_targets.append(await self._user_messages.validate(session, loop, round_row, grant, action.intent_id))
                except UserMessageDeliveryRejected as exc:
                    raise KernelRejected(str(exc)) from exc
        wait_actions = tuple(action for action in intent.actions if action.action == "wait_for_user" and action.cause != "awaiting_input")
        if wait_actions:
            facts = await self._clarification_facts.read(session, loop, grant, round_row)
            for action in wait_actions:
                try:
                    self._clarification_policy.validate(action, facts)
                except ClarificationRejected as exc:
                    raise KernelRejected(str(exc)) from exc
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
        ] + message_targets
        if len(direct_targets) != len(set(direct_targets)):
            raise KernelRejected("同一 round 不得向同一 Context 派发多个并行 Run")
        unsafe_while_active = {"update_lane", "merge_contexts", "pause_lane"}
        if active and any(action.action in unsafe_while_active for action in intent.actions):
            raise KernelRejected("Loop 已有活动 Run")
        created = tuple(action for action in intent.actions if action.action == "create_lane")
        if active and int(active) + len(created) > int(budgets.get("max_concurrent_runs", 4)):
            raise KernelRejected("并行 Run 预算已耗尽")
        for action in created:
            workspace_mode = str(action.plan.lane_policy.get("workspace_mode") or "read_only")
            if workspace_mode == "isolated_write" and (
                "write" not in set(grant.permission_scope or [])
                or "adopt_workspace_result" not in set(grant.capabilities or [])
            ):
                raise KernelRejected("隔离写入派生缺少 workspace write/adoption 授权")
        if active and self._has_adoption(intent):
            raise KernelRejected("仍有活动 Run，不能采用隔离 workspace 结果")
        if active and any(action.action == "stop_loop" for action in intent.actions):
            raise KernelRejected("仍有活动 Run，Patrol 必须先等待本轮安全收敛再停止 Loop")
        human_gate = await session.scalar(select(LoopPendingDecision.pending_decision_id).where(LoopPendingDecision.loop_id == loop.loop_id, LoopPendingDecision.status == "pending", LoopPendingDecision.delegable.is_(False)).limit(1))
        if human_gate and not all(action.action == "wait_for_user" for action in intent.actions):
            raise KernelRejected("存在不可委托人工 gate")
        if self._has_lane_mutation(intent):
            allowed = {"continue_context", "create_lane", "update_lane", "merge_contexts", "pause_lane", "discard_membership", "apply_user_inputs", "cancel_execution"}
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
            from backend.app.desktop.patrol.agent_loop.workspaces.workspace_repair import WorkspaceConflictRepair

            await WorkspaceConflictRepair(self._sessions).validate_result(session, source)
        repairs = [action for action in intent.actions if action.action == "continue_context" and action.workspace_repair]
        if repairs:
            if len(intent.actions) != 1 or active:
                raise KernelRejected("workspace 修复/审查必须在无活动 Run 时独立提交")
            from backend.app.desktop.patrol.agent_loop.workspaces.workspace_repair import WorkspaceConflictRepair

            await WorkspaceConflictRepair(self._sessions).select(session, loop, grant, round_row, repairs[0],
                {item.context_id: item.access_mode for item in intent.context_permissions})
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

    async def _validate_lane_sources(self, session, loop, grant, intent) -> None:
        scope = set(grant.context_scope)
        for action in intent.actions:
            if action.action not in {"create_lane", "update_lane", "merge_contexts"}:
                continue
            if action.action == "merge_contexts" and len(action.plan.source_frontier) < 2:
                raise KernelRejected("merge_contexts 至少需要两个精确来源 revision")
            recovery_id = str(action.plan.lane_policy.get("recovery_opportunity_id") or "")
            if recovery_id:
                await self._validate_recovery_opportunity(
                    session,
                    loop,
                    grant,
                    intent,
                    action.plan,
                    recovery_id,
                )
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

    async def _validate_recovery_opportunity(
        self,
        session,
        loop,
        grant,
        intent,
        plan,
        opportunity_id: str,
    ) -> None:
        opportunity = await self._recovery_opportunities.get_contract(session, opportunity_id)
        if opportunity is None:
            raise KernelRejected("Context recovery opportunity 不存在或已消费")
        current = await session.get(DesktopThread, opportunity.source.context_id)
        try:
            self._recovery_authority.validate(
                opportunity,
                loop_id=loop.loop_id,
                goal_revision=loop.goal_revision,
                workspace_revision=intent.observed_workspace_revision,
                authority_revision=loop.authority_revision,
                grant_id=grant.grant_id,
                grant_revision=grant.revision,
                frontier_hash=intent.observed_frontier_hash,
                current_source_revision_id=(
                    current.current_revision_id if current is not None else None
                ),
                plan=plan,
            )
        except ContextRecoveryAuthorityError as exc:
            raise KernelRejected(str(exc)) from exc

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

    async def _authorize_expansions(self, session: AsyncSession, intent: PatrolDecisionIntent, decision_id: str) -> None:
        for action in intent.actions:
            if action.action != "create_lane":
                continue
            expansion_id = action.plan.lane_policy.get("expansion_id")
            if not expansion_id:
                continue
            row = await session.get(LoopContextExpansion, str(expansion_id))
            if row is not None and row.state == "compiled":
                await self._expansions.transition(
                    session,
                    row.expansion_id,
                    "authorized",
                    "Kernel 已授权 Context expansion publication",
                    result={"decision_id": decision_id},
                )

    async def _fail_deferred(
        self,
        decision_id: str,
        reason: str,
        *,
        superseded: bool = False,
    ) -> KernelCommitResult:
        async with self._sessions.begin() as session:
            decision = await session.get(LoopDecision, decision_id)
            if decision is None:
                raise KernelRejected("待恢复的 Portfolio decision 不存在")
            loop = await session.get(AgentLoop, decision.loop_id, with_for_update=True, populate_existing=True)
            round_row = await session.get(LoopRound, decision.round_id, with_for_update=True, populate_existing=True)
            decision = await session.get(LoopDecision, decision_id, with_for_update=True, populate_existing=True)
            if decision is None:
                raise KernelRejected("待恢复的 Portfolio decision 不存在")
            if decision.status not in {"publishing", "adopting"}:
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
            await self._terminate_expansions(
                session,
                decision.intent,
                "superseded" if superseded else "failed",
                reason,
            )
            if round_row is not None:
                await terminate_round(session, loop, round_row, category="deferred_superseded" if superseded else "deferred_failure",
                    reason=reason, decision_id=decision_id, allowed_statuses={"publishing", "adopting"},
                    terminal_status="superseded" if superseded else "error", wait_for_user=False)
            if (loop is not None and loop.status == "running"
                and loop.current_round_id == decision.round_id):
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

    async def _terminate_expansions(self, session: AsyncSession, raw_intent: dict, target: str, reason: str) -> None:
        intent = PatrolDecisionIntent.model_validate(raw_intent)
        for action in intent.actions:
            if action.action != "create_lane":
                continue
            expansion_id = action.plan.lane_policy.get("expansion_id")
            expansion = await session.get(LoopContextExpansion, str(expansion_id), with_for_update=True) if expansion_id else None
            if expansion is not None and not self._expansions.is_terminal(expansion.state):
                await self._expansions.transition(
                    session,
                    expansion.expansion_id,
                    target,
                    reason[:1000],
                )

    async def _apply_actions(self, session, loop, round_row, grant, decision, intent, input_results=None):
        action_ids: list[str] = []
        directive_ids: list[str] = []
        deferred_inputs = tuple(item.intent_id for action in intent.actions if action.action == "apply_user_inputs"
            for item in action.inputs if item.disposition == "deferred")
        for position, intent_action in enumerate(intent.actions):
            action_id = uuid.uuid4().hex
            action = LoopAction(action_id=action_id, decision_id=decision.decision_id, loop_id=loop.loop_id, position=position, action_type=intent_action.action, payload=intent_action.model_dump(mode="json"), status="committed")
            session.add(action)
            action_ids.append(action_id)
            if intent_action.action == "deliver_user_message":
                directive, provenance = await self._user_messages.create(
                    session, loop, round_row, grant, decision, action, intent_action.intent_id)
                session.add_all([directive, provenance])
                await session.flush()
                await self._directive_lifecycle.register(session, directive)
                await self._directive_lifecycle.transition(session, directive.directive_id, "authorized")
                directive_ids.append(directive.directive_id)
            elif intent_action.action == "apply_user_inputs":
                action.result = (input_results or {})[position]
                action.status = "applied"
            elif intent_action.action == "cancel_execution":
                from backend.app.desktop.patrol.agent_loop.execution.execution_cancellation import ExecutionCancellation

                observation = await session.get(LoopObservation, round_row.observation_id) if round_row.observation_id else None
                action.result = await ExecutionCancellation().apply(session, loop, observation, intent_action, action_id)
                action.status = "applied"
            elif intent_action.action == "continue_context":
                content = intent_action.message
                if intent_action.workspace_repair:
                    from backend.app.desktop.patrol.agent_loop.workspaces.workspace_repair import WorkspaceConflictRepair

                    repair = WorkspaceConflictRepair(self._sessions)
                    from backend.app.desktop.workspaces.coordination.models import WorkspaceAdoption

                    adoption = await session.get(WorkspaceAdoption, intent_action.workspace_repair.adoption_id)
                    context = adoption.evidence["repair_context"]
                    action.result = {"repair_context": context}
                    content = repair.message(context, intent_action.workspace_repair.phase, content)
                directive, provenance = self._directives.create(
                    loop_id=loop.loop_id, round_id=round_row.round_id, decision_id=decision.decision_id,
                    action_id=action_id, context_id=intent_action.context_id,
                    context_revision_id=intent_action.context_revision_id, content=content,
                    actor_id=intent.holder_id, grant_id=grant.grant_id, grant_revision=grant.revision,
                    goal_revision=loop.goal_revision, idempotency_key=f"{intent.idempotency_key}:directive:{position}",
                    origin_kind=intent.origin_kind,
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
            elif intent_action.action == "decline_expansion":
                action.status = "applied"
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
                scope = intent_action.model_dump(mode="json")
                if intent_action.action == 'request_completion_verifier':
                    from backend.app.desktop.patrol.agent_loop.completion.completion_admission import CompletionRequestAdmission

                    admission = await CompletionRequestAdmission().read(session, loop, round_row)
                    CompletionRequestAdmission.require_allowed(admission)
                    scope['completion_admission_input'] = admission['input']
                session.add(LoopWorkerRequest(worker_request_id=uuid.uuid4().hex, loop_id=loop.loop_id, round_id=round_row.round_id, observation_id=round_row.observation_id, observation_generation=intent.observation_generation, kind=intent_action.action.removeprefix("request_"), scope=scope))
            elif intent_action.action == "wait_for_user":
                if intent_action.cause == "awaiting_input":
                    await self._park_workspace(session, loop, deferred_inputs=deferred_inputs)
                    continue
                await LoopWaitRequestService().open(
                    session,
                    loop,
                    LoopWaitRequestFactory.clarification("\n".join(filter(None, (intent_action.reason, intent_action.required_input))), {
                        "decision_id": decision.decision_id,
                        "cause": intent_action.cause,
                        "required_input": intent_action.required_input,
                        "evidence_identity": intent_action.evidence_identity.model_dump(mode="json") if intent_action.evidence_identity else None,
                        "mission_revision": loop.goal_revision,
                    }),
                    created_by="portfolio-patrol",
                    correlation_id=decision.decision_id,
                    round_id=round_row.round_id,
                )
                if loop.interaction_mode == "workspace_patrol" and intent_action.cause == "missing_input":
                    await self._park_workspace(session, loop, required=False, deferred_inputs=deferred_inputs)
            elif intent_action.action == "stop_loop":
                await self._terminal.finalize(session, loop, "stopped", "patrol_stop_loop")
            elif intent_action.action == "request_completion":
                final_result = await self._completion_result(session, loop, intent_action)
                if loop.interaction_mode == "workspace_patrol":
                    loop.final_result = final_result
                    await self._park_workspace(session, loop)
                    continue
                await self._terminal.finalize(
                    session,
                    loop,
                    "completed",
                    "patrol_request_completion",
                    final_result=final_result,
                )
        return action_ids, directive_ids

    @staticmethod
    async def _park_workspace(session, loop, *, required=True, deferred_inputs=()):
        if loop.interaction_mode != "workspace_patrol":
            raise KernelRejected("等待新输入只适用于工作区 Patrol")
        await session.flush()
        from backend.app.desktop.patrol.agent_loop.inputs.patrol_inputs import workspace_input_scope
        pending = await session.scalar(select(LoopUserIntent.intent_id).where(workspace_input_scope(loop),
            LoopUserIntent.intent_id.not_in(deferred_inputs),
            LoopUserIntent.status.in_(("pending", "observed"))).limit(1))
        active = await session.scalar(select(DesktopRun.run_id).where(DesktopRun.loop_id == loop.loop_id,
            DesktopRun.status.in_(("pending", "running"))).limit(1))
        gate = await session.scalar(select(LoopPendingDecision.pending_decision_id).where(
            LoopPendingDecision.loop_id == loop.loop_id, LoopPendingDecision.status == "pending").limit(1))
        worker = await session.scalar(select(LoopWorkerRequest.worker_request_id).where(
            LoopWorkerRequest.loop_id == loop.loop_id, LoopWorkerRequest.status.in_(("pending", "running"))).limit(1))
        directive = await session.scalar(select(LoopDirective.directive_id).where(
            LoopDirective.loop_id == loop.loop_id, LoopDirective.lifecycle_state.in_(("authorized", "delivering", "delivered", "run_started"))).limit(1))
        from backend.app.desktop.patrol.agent_loop.waits.wait_models import LoopWaitRequest
        safety_wait = await session.scalar(select(LoopWaitRequest.request_id).where(LoopWaitRequest.loop_id == loop.loop_id,
            LoopWaitRequest.status.in_(("open", "resolving")),
            func.coalesce(LoopWaitRequest.scope["information_only"].astext, "false") != "true").limit(1))
        if any((pending, active, gate, worker, directive, safety_wait)):
            if not required:
                return False
            raise KernelRejected("仍有待处理输入、Run、请求或执行后果，不能等待新输入")
        loop.status, loop.health, loop.waiting_reason = "waiting_user", "idle", "awaiting_input"
        return True

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
        from backend.app.desktop.patrol.agent_loop.missions.mission_repository import MissionRevisionRepository
        from backend.app.desktop.patrol.agent_loop.missions.mission_projection import EffectiveMissionProjector
        from backend.app.desktop.patrol.agent_loop.models import LoopGoalRevision

        verification = await session.get(CompletionVerification, action.verification_id)
        mission = await MissionRevisionRepository().get(session, loop.loop_id, loop.goal_revision)
        goal = await session.scalar(select(LoopGoalRevision).where(
            LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == loop.goal_revision))
        effective = EffectiveMissionProjector.from_rows(structured=mission, legacy=goal)
        contexts = {item.task_id: item for item in (await session.scalars(
            select(DesktopThread).where(DesktopThread.task_id.in_(action.final_context_ids)))).all()}
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
            "summary": effective.outcome,
            "goal_revision": loop.goal_revision,
            "round_id": loop.current_round_id,
            "completion_checks": list(effective.completion_checks),
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
                    "revision_id": contexts[item.context_id].current_revision_id,
                    "title": contexts[item.context_id].title,
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
        from backend.app.desktop.patrol.agent_loop.completion.completion_eligibility import CompletionEligibilityPolicy, CompletionEligibilityRejected

        try:
            await CompletionEligibilityPolicy().require_current(session, loop, round_row, action)
        except CompletionEligibilityRejected as exc:
            raise KernelRejected(str(exc)) from exc

    @staticmethod
    def _rejection_reason(intent: PatrolDecisionIntent, exc: Exception) -> str:
        if any(action.action == "request_completion" for action in intent.actions):
            return f"Completion Guard 未通过: {str(exc)[:1000]}"
        return str(exc)

    @staticmethod
    def _decision(intent: PatrolDecisionIntent, status: str, rejection: dict) -> LoopDecision:
        return LoopDecision(decision_id=intent.decision_id, loop_id=intent.loop_id, round_id=intent.round_id, holder_id=intent.holder_id, intent=intent.model_dump(mode="json"), rationale=intent.rationale, evidence=list(intent.evidence), status=status, idempotency_key=intent.idempotency_key, rejection=rejection, fencing_token=intent.fencing_token, observation_id=intent.observation_id, observation_generation=intent.observation_generation)

    @staticmethod
    def _round_status(intent: PatrolDecisionIntent) -> str:
        if any(action.action == "apply_context_compression" for action in intent.actions):
            return "resolving_gate"
        if any(action.action in {"request_lane_curator", "request_completion_verifier"} for action in intent.actions):
            return "waiting_workers"
        if any(action.action in {"continue_context", "deliver_user_message", "create_lane", "update_lane", "merge_contexts"} for action in intent.actions):
            return "ready"
        return "settled"

    @staticmethod
    def _health(intent: PatrolDecisionIntent) -> str:
        if any(action.action == "apply_context_compression" for action in intent.actions):
            return "resuming"
        if any(action.action in {"request_lane_curator", "request_completion_verifier"} for action in intent.actions):
            return "waiting_workers"
        if any(action.action in {"continue_context", "deliver_user_message", "create_lane", "update_lane", "merge_contexts"} for action in intent.actions):
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
