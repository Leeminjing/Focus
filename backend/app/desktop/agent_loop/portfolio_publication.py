r"""本文件对外提供 LoopPortfolioPublicationService 与 LoopPortfolioAuthorityHook。

输入为 Kernel 已授权的 Loop decision、结构化 Lane plan、精确多来源 evidence 和冻结控制版本；输出为
原子发布的 Portfolio revision、Loop membership、Expansion transition 与 delegated directives。具体工作流为预登记稳定 Lane 与
Decision 唯一的 Portfolio identity，调用统一 compiler 生成候选，在 shadow checkpoint 幂等准备全部 Context revision，再由 authority hook 在
AtomicPortfolioPublisher 的每次独立事务尝试内重验 Loop/grant/workspace，并提交所有 Loop 侧指针、指令、Context projection 与
`portfolio.published` 规范事件与成员派生事实；服务只从最终已提交数据库事实返回 Directive identity，失败重试的内存状态不会泄漏。
示例：`result = await service.publish(decision_id)`。
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import (
    AgentLoop,
    LoopAction,
    LoopBudgetUsage,
    LoopContextMembership,
    LoopDecision,
    LoopDelegationGrant,
    LoopDirective,
    LoopEventOutbox,
    LoopRound,
)
from backend.app.desktop.agent_loop.context_expansion.models import LoopContextExpansion
from backend.app.desktop.agent_loop.context_expansion.repository import ContextExpansionRepository
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.provenance import DelegatedDirectiveFactory
from backend.app.desktop.agent_loop.ownership import LoopFencingGuard
from backend.app.desktop.agent_loop.portfolio_events import ContextPublicationEventRecorder, PortfolioPublicationEventRecorder
from backend.app.desktop.agent_loop.lineage_events import ContextLineageEventRecorder
from backend.app.desktop.agent_loop.schemas import PATROL_ACTION_ADAPTER
from backend.app.desktop.context_curation import (
    AtomicPortfolioPublisher,
    CurationLane,
    CurationProgram,
    CurationProgramRepository,
    MultiSourceEvidence,
    NamespacedMessageRef,
    FrozenPortfolio,
    PortfolioAuthorityCommitHook,
    PortfolioCandidatePreparer,
    PortfolioControlRevisions,
    PortfolioFreezeRequest,
    PortfolioFreezer,
    PortfolioLaneAction,
    PortfolioLaneCandidate,
    PortfolioLaneIntent,
    PortfolioPublicationResult,
    PortfolioRevision,
    PortfolioSuperseded,
    SourceMessageEvidence,
    SourceRevisionEvidence,
    compile_lane,
)
from backend.app.desktop.context_evolution import (
    ContextRevisionPublisher,
    ContextRevisionReader,
    ContextRevisionRef,
    ContextRevisionRepository,
    LangGraphContextCheckpointWriter,
)
from backend.app.desktop.models import DesktopThread
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


_LANE_MUTATIONS = frozenset({"create_lane", "update_lane", "merge_contexts"})


@dataclass(frozen=True, slots=True)
class LoopPortfolioPublishResult:
    portfolio: PortfolioPublicationResult
    directive_ids: tuple[str, ...]


class LoopPortfolioAuthorityHook(PortfolioAuthorityCommitHook):
    def __init__(self, decision_id: str) -> None:
        self._decision_id = decision_id
        self._directives = DelegatedDirectiveFactory()
        self._directive_ids: list[str] = []
        self._directive_lifecycle = DirectiveLifecycleRepository()
        self._expansions = ContextExpansionRepository()
        self._context_events = ContextPublicationEventRecorder()
        self._lineage_events = ContextLineageEventRecorder()

    @property
    def directive_ids(self) -> tuple[str, ...]:
        return tuple(self._directive_ids)

    def begin_attempt(self) -> None:
        self._directive_ids.clear()

    async def lock_authority(self, session: AsyncSession) -> None:
        decision = await session.get(LoopDecision, self._decision_id)
        if decision is None:
            raise PortfolioSuperseded("Loop decision 已失效")
        loop = await session.get(AgentLoop, decision.loop_id, with_for_update=True)
        round_row = await session.get(LoopRound, decision.round_id, with_for_update=True)
        if loop is None or round_row is None:
            raise PortfolioSuperseded("Loop/round authority identity 已变化")
        await session.scalar(
            select(LoopDelegationGrant)
            .where(
                LoopDelegationGrant.loop_id == loop.loop_id,
                LoopDelegationGrant.revision == loop.authority_revision,
            )
            .with_for_update()
        )

    async def commit(
        self,
        session: AsyncSession,
        portfolio: PortfolioRevision,
        candidates: tuple[PortfolioLaneCandidate, ...],
        revisions: tuple[ContextRevisionRef, ...],
        controls: PortfolioControlRevisions,
    ) -> None:
        decision = await session.get(LoopDecision, self._decision_id, with_for_update=True)
        if decision is None or decision.status not in {"publishing", "publishing_run"}:
            raise PortfolioSuperseded("Loop decision 已失效")
        if decision.fencing_token:
            await LoopFencingGuard().validate_current(session, decision.round_id, decision.fencing_token)
        loop = await session.get(AgentLoop, decision.loop_id, with_for_update=True)
        round_row = await session.get(LoopRound, decision.round_id, with_for_update=True)
        if loop is None or round_row is None or round_row.decision_id != decision.decision_id:
            raise PortfolioSuperseded("Loop/round authority identity 已变化")
        grant = await session.scalar(
            select(LoopDelegationGrant)
            .where(
                LoopDelegationGrant.loop_id == loop.loop_id,
                LoopDelegationGrant.revision == loop.authority_revision,
            )
            .with_for_update()
        )
        await self._validate_authority(session, loop, round_row, grant, controls)
        actions = list(
            (
                await session.scalars(
                    select(LoopAction)
                    .where(LoopAction.decision_id == decision.decision_id)
                    .order_by(LoopAction.position)
                    .with_for_update()
                )
            ).all()
        )
        by_lane = {candidate.lane_id: candidate for candidate in candidates}
        intent = decision.intent
        for action_row, raw_action in zip(actions, intent["actions"]):
            parsed = PATROL_ACTION_ADAPTER.validate_python(raw_action)
            if parsed.action in _LANE_MUTATIONS:
                await self._commit_lane(session, loop, round_row, grant, decision, action_row, parsed, by_lane)
            elif parsed.action == "continue_context":
                await self._add_directive(
                    session, loop, round_row, grant, decision, action_row,
                    parsed.context_id, parsed.context_revision_id, parsed.message,
                )
                action_row.status = "applied"
                action_row.result = {
                    **action_row.result,
                    "directive_id": self._directive_ids[-1],
                }
            elif parsed.action == "pause_lane":
                await self._set_memberships(session, loop.loop_id, parsed.lane_id, "paused")
                action_row.status = "applied"
            elif parsed.action == "discard_membership":
                membership = await session.get(LoopContextMembership, parsed.membership_id, with_for_update=True)
                if membership is None or membership.loop_id != loop.loop_id:
                    raise PortfolioSuperseded("待淘汰 membership 已变化")
                membership.status = "discarded"
                action_row.status = "applied"
            else:
                raise PortfolioSuperseded("Portfolio publication 不接受混合终止或 Worker action")
        loop.current_portfolio_revision_id = portfolio.portfolio_revision_id
        usage = await session.get(LoopBudgetUsage, loop.loop_id, with_for_update=True)
        if usage is not None:
            usage.lanes = int(
                await session.scalar(
                    select(func.count(func.distinct(LoopContextMembership.lane_id))).where(
                        LoopContextMembership.loop_id == loop.loop_id,
                        LoopContextMembership.status == "active",
                        LoopContextMembership.lane_id.is_not(None),
                    )
                )
                or 0
            )
        loop.health = "dispatching" if self._directive_ids else "idle"
        round_row.status = "ready" if self._directive_ids else "settled"
        decision.status = "committed"
        await self._event(
            session,
            loop.loop_id,
            "LoopDecisionCommitted",
            {"decision_id": decision.decision_id, "round_id": round_row.round_id, "portfolio_revision_id": portfolio.portfolio_revision_id},
            f"decision:{decision.decision_id}",
        )
        await PortfolioPublicationEventRecorder().record(
            session,
            loop_id=loop.loop_id,
            portfolio_id=portfolio.portfolio_revision_id,
            generation=portfolio.generation,
            round_id=round_row.round_id,
            decision_id=decision.decision_id,
            directive_ids=tuple(self._directive_ids),
        )
        await self._lineage_events.record_loop_members(session, loop_id=loop.loop_id)

    @staticmethod
    async def _validate_authority(session, loop, round_row, grant, controls) -> None:
        if loop.status != "running" or grant is None or grant.status != "active":
            raise PortfolioSuperseded("Loop delegation 已撤销")
        if loop.revision != controls.loop_revision or grant.revision != controls.grant_revision:
            raise PortfolioSuperseded("Loop/grant revision 已变化")
        if round_row.authority_revision != grant.revision or round_row.goal_revision != loop.goal_revision:
            raise PortfolioSuperseded("round authority/goal revision 已变化")
        slot = await session.scalar(
            select(WorkspaceSlot)
            .where(
                WorkspaceSlot.workspace_id == loop.workspace_id,
                WorkspaceSlot.kind == "authoritative",
                WorkspaceSlot.lifecycle != "deleted",
            )
            .with_for_update()
        )
        if slot is None or str(slot.revision) != controls.workspace_revision:
            raise PortfolioSuperseded("authoritative workspace revision 已变化")

    async def _commit_lane(self, session, loop, round_row, grant, decision, action_row, action, by_lane) -> None:
        lane_id = str(action_row.result["lane_id"])
        candidate = by_lane.get(lane_id)
        if candidate is None or candidate.target_context_id is None or candidate.candidate_context_revision_id is None:
            raise PortfolioSuperseded("Lane candidate publication 不完整")
        old = list(
            (
                await session.scalars(
                    select(LoopContextMembership)
                    .where(
                        LoopContextMembership.loop_id == loop.loop_id,
                        LoopContextMembership.lane_id == lane_id,
                        LoopContextMembership.status == "active",
                    )
                    .with_for_update()
                )
            ).all()
        )
        for membership in old:
            if membership.context_id != candidate.target_context_id:
                membership.status = "superseded"
        membership = await session.scalar(
            select(LoopContextMembership)
            .where(
                LoopContextMembership.loop_id == loop.loop_id,
                LoopContextMembership.context_id == candidate.target_context_id,
            )
            .with_for_update()
        )
        if membership is None:
            membership = LoopContextMembership(
                membership_id=uuid.uuid4().hex,
                loop_id=loop.loop_id,
                context_id=candidate.target_context_id,
                lane_id=lane_id,
                role="synthesis" if action.action == "merge_contexts" else "side",
            )
            session.add(membership)
        else:
            membership.lane_id = lane_id
            membership.status = "active"
        grant.context_scope = list(dict.fromkeys([*grant.context_scope, candidate.target_context_id]))
        await self._add_directive(
            session, loop, round_row, grant, decision, action_row,
            candidate.target_context_id, candidate.candidate_context_revision_id, action.message,
        )
        action_row.status = "applied"
        action_row.result = {
            **action_row.result,
            "context_id": candidate.target_context_id,
            "context_revision_id": candidate.candidate_context_revision_id,
            "directive_id": self._directive_ids[-1],
        }
        expansion_id = action.plan.lane_policy.get("expansion_id")
        if expansion_id:
            expansion = await session.get(LoopContextExpansion, str(expansion_id), with_for_update=True)
            if expansion is not None and expansion.state == "authorized":
                await self._expansions.transition(
                    session,
                    expansion.expansion_id,
                    "committed",
                    "Context、Lane、Membership、Grant Scope 与 Directive 已原子提交",
                    result={
                        "lane_id": lane_id,
                        "context_id": candidate.target_context_id,
                        "context_revision_id": candidate.candidate_context_revision_id,
                        "directive_id": self._directive_ids[-1],
                        "portfolio_revision_id": candidate.portfolio_revision_id,
                    },
                )
                context = await session.get(DesktopThread, candidate.target_context_id)
                if context is not None:
                    await self._context_events.record(
                        session,
                        loop_id=loop.loop_id,
                        decision_id=decision.decision_id,
                        context=context,
                        membership=membership,
                    )

    async def _add_directive(self, session, loop, round_row, grant, decision, action_row, context_id, revision_id, message) -> None:
        directive, provenance = self._directives.create(
            loop_id=loop.loop_id,
            round_id=round_row.round_id,
            decision_id=decision.decision_id,
            action_id=action_row.action_id,
            context_id=context_id,
            context_revision_id=revision_id,
            content=message,
            actor_id=loop.holder_id,
            grant_id=grant.grant_id,
            grant_revision=grant.revision,
            goal_revision=loop.goal_revision,
            idempotency_key=f"{decision.idempotency_key}:directive:{action_row.position}",
        )
        session.add_all([directive, provenance])
        await self._directive_lifecycle.register(session, directive)
        await self._directive_lifecycle.transition(session, directive.directive_id, "authorized")
        self._directive_ids.append(directive.directive_id)

    @staticmethod
    async def _set_memberships(session, loop_id: str, lane_id: str, status: str) -> None:
        rows = list(
            (
                await session.scalars(
                    select(LoopContextMembership)
                    .where(LoopContextMembership.loop_id == loop_id, LoopContextMembership.lane_id == lane_id)
                    .with_for_update()
                )
            ).all()
        )
        for row in rows:
            row.status = status

    @staticmethod
    async def _event(session, loop_id: str, event_type: str, payload: dict, key: str) -> None:
        sequence = await session.scalar(
            select(func.coalesce(func.max(LoopEventOutbox.sequence), 0)).where(LoopEventOutbox.loop_id == loop_id)
        )
        session.add(
            LoopEventOutbox(
                event_id=uuid.uuid4().hex,
                loop_id=loop_id,
                sequence=int(sequence or 0) + 1,
                event_type=event_type,
                payload=payload,
                idempotency_key=key,
            )
        )


class LoopPortfolioPublicationService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], context_service) -> None:
        self._sessions = sessions
        self._revisions = ContextRevisionRepository()
        self._reader = ContextRevisionReader(self._revisions, context_service.checkpointer)
        writer = LangGraphContextCheckpointWriter(context_service.make_state_graph, context_service.checkpointer)
        self._freezer = PortfolioFreezer(sessions, self._revisions)
        self._preparer = PortfolioCandidatePreparer(
            sessions,
            ContextRevisionPublisher(self._revisions, writer),
            self._revisions,
        )
        self._publisher = AtomicPortfolioPublisher(sessions, self._revisions)

    async def publish(self, decision_id: str) -> LoopPortfolioPublishResult:
        request, compiled, portfolio_id = await self._build(decision_id)
        frozen = (
            await self._frozen(portfolio_id)
            if request is None
            else await self._freezer.freeze(
                request,
                portfolio_revision_id=portfolio_id,
            )
        )
        by_lane = await self._candidate_ids(frozen.portfolio_revision_id)
        if await self._portfolio_status(frozen.portfolio_revision_id) == "preparing":
            await self._preparer.prepare(
                frozen.portfolio_revision_id,
                {by_lane[lane_id]: candidate for lane_id, candidate in compiled.items()},
            )
        hook = LoopPortfolioAuthorityHook(decision_id)
        result = await self._publisher.publish(
            frozen.portfolio_revision_id,
            frozen.controls,
            hook,
        )
        return LoopPortfolioPublishResult(result, await self._committed_directive_ids(decision_id))

    async def _committed_directive_ids(self, decision_id: str) -> tuple[str, ...]:
        async with self._sessions() as session:
            return tuple(
                (
                    await session.scalars(
                        select(LoopDirective.directive_id)
                        .where(LoopDirective.decision_id == decision_id)
                        .order_by(LoopDirective.created_at, LoopDirective.directive_id)
                    )
                ).all()
            )

    async def _build(self, decision_id: str):
        async with self._sessions.begin() as session:
            decision = await session.get(LoopDecision, decision_id, with_for_update=True)
            if decision is None:
                raise PortfolioSuperseded("Loop decision 不存在")
            actions = list(
                (
                    await session.scalars(
                        select(LoopAction)
                        .where(LoopAction.decision_id == decision_id)
                        .order_by(LoopAction.position)
                        .with_for_update()
                    )
                ).all()
            )
            existing_id = next(
                (
                    str(item.result["portfolio_revision_id"])
                    for item in actions
                    if item.result.get("portfolio_revision_id")
                ),
                None,
            )
            if decision.status == "committed":
                if existing_id is None:
                    raise PortfolioSuperseded("已提交 Loop decision 缺少 Portfolio identity")
                return None, {}, existing_id
            if decision.status not in {"publishing", "publishing_run"}:
                raise PortfolioSuperseded("Loop decision 不处于 publishing")
            loop = await session.get(AgentLoop, decision.loop_id, with_for_update=True)
            round_row = await session.get(LoopRound, decision.round_id, with_for_update=True)
            program = await session.get(CurationProgram, loop.program_id, with_for_update=True) if loop else None
            if loop is None or round_row is None or program is None:
                raise PortfolioSuperseded("Loop publication identity 不完整")
            parsed = [PATROL_ACTION_ADAPTER.validate_python(item) for item in decision.intent["actions"]]
            changed, compiled = await self._ensure_lanes(session, loop, actions, parsed)
            lanes = list(
                (
                    await session.scalars(
                        select(CurationLane)
                        .where(CurationLane.program_id == program.program_id, CurationLane.lifecycle != "retired")
                        .order_by(CurationLane.lane_id)
                        .with_for_update()
                    )
                ).all()
            )
            pause_ids = {item.lane_id for item in parsed if item.action == "pause_lane"}
            intents: list[PortfolioLaneIntent] = []
            frontier: dict[str, ContextRevisionRef] = {}
            for lane in lanes:
                mutation = changed.get(lane.lane_id)
                if mutation is not None:
                    refs = mutation.plan.source_frontier
                    action = PortfolioLaneAction.CREATE if lane.managed_context_id is None else PortfolioLaneAction.UPDATE
                    semantic = compiled[lane.lane_id].semantic_fingerprint
                elif lane.lane_id in pause_ids:
                    refs = ()
                    action = PortfolioLaneAction.PAUSE
                    semantic = lane.current_semantic_fingerprint
                else:
                    current = await self._revisions.current(session, lane.managed_context_id) if lane.managed_context_id else None
                    if current is None:
                        continue
                    refs = (current.ref,)
                    action = PortfolioLaneAction.KEEP
                    semantic = lane.current_semantic_fingerprint or current.content_hash
                for ref in refs:
                    existing = frontier.get(ref.context_id)
                    if existing is not None and existing != ref:
                        raise PortfolioSuperseded("同一 Portfolio frontier 不能绑定同一 Context 的两个 revision")
                    frontier[ref.context_id] = ref
                intents.append(
                    PortfolioLaneIntent(
                        lane_id=lane.lane_id,
                        action=action,
                        purpose=lane.purpose,
                        source_allocation=tuple(refs),
                        semantic_fingerprint=semantic,
                    )
                )
            request = PortfolioFreezeRequest(
                program_id=program.program_id,
                source_frontier=tuple(frontier.values()),
                lane_intents=tuple(intents),
                workspace_revision=str(round_row.workspace_revision),
                loop_revision=loop.revision,
                grant_revision=loop.authority_revision,
            )
            if existing_id is None:
                existing_id = self._portfolio_identity(decision_id)
                for action in actions:
                    action.result = {
                        **action.result,
                        "portfolio_revision_id": existing_id,
                    }
            return request, compiled, existing_id

    @staticmethod
    def _portfolio_identity(decision_id: str) -> str:
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"focus:loop-portfolio:{decision_id}",
        ).hex

    async def _ensure_lanes(self, session, loop, action_rows, actions):
        changed = {}
        compiled = {}
        for row, action in zip(action_rows, actions):
            if action.action not in _LANE_MUTATIONS:
                continue
            plan = action.plan
            candidate = compile_lane(plan, await self._evidence(session, plan.source_frontier))
            remembered_lane_id = row.result.get("lane_id")
            if remembered_lane_id:
                lane = await session.get(CurationLane, str(remembered_lane_id), with_for_update=True)
                if lane is None or lane.program_id != loop.program_id:
                    raise PortfolioSuperseded("已登记 Lane identity 已失效")
            elif action.action == "update_lane":
                lane = await session.get(CurationLane, plan.lane_id, with_for_update=True)
                if lane is None or lane.program_id != loop.program_id:
                    raise PortfolioSuperseded("待更新 Lane 不属于当前 Loop")
                current = await self._revisions.current(session, lane.managed_context_id) if lane.managed_context_id else None
                if current is None or current.ref != plan.base_context_revision:
                    raise PortfolioSuperseded("Lane base Context revision 已变化")
                if lane.publisher_epoch != plan.publisher_epoch:
                    raise PortfolioSuperseded("Lane publisher epoch 已变化")
                if CurationProgramRepository.normalize_purpose(plan.purpose) != lane.normalized_purpose:
                    raise PortfolioSuperseded("update_lane 不得隐式改变稳定 Lane purpose")
            else:
                normalized = CurationProgramRepository.normalize_purpose(plan.purpose)
                semantic_fingerprint = str(plan.lane_policy.get("semantic_fingerprint") or "")
                duplicate = await session.scalar(
                    select(CurationLane).where(
                        CurationLane.program_id == loop.program_id,
                        (
                            (CurationLane.normalized_purpose == normalized)
                            | (CurationLane.current_semantic_fingerprint == semantic_fingerprint)
                        ),
                        CurationLane.lifecycle != "retired",
                    )
                )
                if duplicate is not None:
                    raise PortfolioSuperseded("新 Lane 与现有活动 purpose 重复，应复用或更新")
                lane = CurationLane(
                    lane_id=uuid.uuid4().hex,
                    program_id=loop.program_id,
                    purpose=plan.purpose,
                    normalized_purpose=normalized,
                    lane_policy=plan.lane_policy,
                    lifecycle="active",
                    publisher_epoch=1,
                )
                session.add(lane)
                await session.flush()
            if action.action == "update_lane" and remembered_lane_id:
                current = await self._revisions.current(session, lane.managed_context_id) if lane.managed_context_id else None
                if (
                    current is None
                    or current.ref != plan.base_context_revision
                    or lane.publisher_epoch != plan.publisher_epoch
                ):
                    raise PortfolioSuperseded("已登记 update_lane 的 base revision 或 publisher epoch 已变化")
            row.result = {**row.result, "lane_id": lane.lane_id}
            changed[lane.lane_id] = action
            compiled[lane.lane_id] = candidate
        return changed, compiled

    async def _evidence(
        self,
        session: AsyncSession,
        frontier: tuple[ContextRevisionRef, ...],
    ) -> MultiSourceEvidence:
        sources: list[SourceRevisionEvidence] = []
        for ref in frontier:
            revision = await self._revisions.get(session, ref)
            view = await self._reader.read(session, ref, "execution")
            messages = tuple(
                SourceMessageEvidence(
                    ref=NamespacedMessageRef(source=ref, message_id=str(message["id"])),
                    role=message.get("role", "human"),
                    content=message.get("content", ""),
                    tool_calls=tuple(message.get("tool_calls") or ()),
                    tool_call_id=message.get("tool_call_id"),
                    name=message.get("name"),
                    status=message.get("status"),
                )
                for message in view.messages
                if message.get("id")
            )
            sources.append(
                SourceRevisionEvidence(
                    source=ref,
                    projection_hash=revision.projection_hash or revision.content_hash,
                    content_hash=revision.content_hash,
                    messages=messages,
                )
            )
        return MultiSourceEvidence(sources=tuple(sources))

    async def _candidate_ids(self, portfolio_id: str) -> dict[str, str]:
        async with self._sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(PortfolioLaneCandidate).where(
                            PortfolioLaneCandidate.portfolio_revision_id == portfolio_id
                        )
                    )
                ).all()
            )
            return {row.lane_id: row.candidate_id for row in rows}

    async def _frozen(self, portfolio_id: str):
        async with self._sessions() as session:
            portfolio = await session.get(PortfolioRevision, portfolio_id)
            if portfolio is None:
                raise PortfolioSuperseded("已登记 Portfolio publication 不存在")
            candidate_ids = tuple(
                (
                    await session.scalars(
                        select(PortfolioLaneCandidate.candidate_id)
                        .where(PortfolioLaneCandidate.portfolio_revision_id == portfolio_id)
                        .order_by(PortfolioLaneCandidate.lane_id)
                    )
                ).all()
            )
            return FrozenPortfolio(
                portfolio_revision_id=portfolio.portfolio_revision_id,
                program_id=portfolio.program_id,
                generation=portfolio.generation,
                source_frontier=tuple(ContextRevisionRef.model_validate(item) for item in portfolio.source_frontier),
                controls=PortfolioControlRevisions.model_validate(portfolio.control_revisions),
                candidate_ids=candidate_ids,
            )

    async def _portfolio_status(self, portfolio_id: str) -> str:
        async with self._sessions() as session:
            status = await session.scalar(
                select(PortfolioRevision.status).where(PortfolioRevision.portfolio_revision_id == portfolio_id)
            )
            if status not in {"preparing", "ready", "published"}:
                raise PortfolioSuperseded(f"Portfolio publication 不可恢复: {status}")
            return str(status)
