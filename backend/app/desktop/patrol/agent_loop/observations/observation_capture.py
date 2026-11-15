"""本文件对外提供 LoopObservationService 与 PatrolReadRequest 的冻结世界和精确读取端口。

输入为持久 Loop/Round、可选新用户输入与精确 Context 内容；输出为不可变版本、三项冻结输入和因果历史，或 ObservationCaptureSuperseded。
具体工作流为事务外准备精确内容，再于一致短事务冻结前序进度、当前世界及真实血缘并发布该 Round 的规范状态；selective_read 仅接受冻结 handles。
新冻结在锁定 Loop/Round 后重验活动状态与授权版本，停止、撤销、过期或已终结的轮次不登记记忆；已冻结记录仍可恢复读取。
示例：await service.capture(loop_id, round_id)。认知产物通过独立 decision_context 装配，不改写基础观察。
直接用户消息冻结类型及原始请求 hash；已观察但未交付的消息继续进入后继观察，原文由耐久请求提供，不由预览重写。
Worker 认知读取复用冻结基础及持久补充来源，精确分页核验当前授权、完整 hash 与所属领域，历史材料不变成本轮准备证明。
新观察冻结同源完成请求资格及语义输入，供 Patrol 反馈与稳定 Round 进展比较；原冻结 hash 不重写。
具体验证的完成资格由共享只读 CompletionEligibilityPolicy 冻结，不从 allowed 新验证推断完成。
工作区 Git 状态在事务外读取，冻结时重验 slot 版本；原 workspace 投影同时提供隔离授权、slot 和 adoption
事实，stable_results 包含执行 slot 锚点，供 Patrol 判断阶段与显式串行采用，不修改物理仓库。
workspace 输入按完整有界集合冻结，未处置和显式延后来源进入后继；同工作区终态 predecessor 输入保留原身份。补充卡片和已受理答案独立冻结，迟到输入不归旧轮处置。
输入事务通过 prepare_views、prepare_state、freeze_version 复用同一证据读取器；冻结不登记沉淀任务，成功授权另行登记。
workspace 输入的 introduced identity 固定原 Oi，observed_round_id 跟随当前处置轮；deferred 跨轮重新观察而不改写原因果。
冻结时从记忆库读取启用的全局/当前工作区策略；file_refs 与精确附件 manifest 随原文及因果链冻结，后续策略编辑和磁盘变化不改旧观察。
"""

from __future__ import annotations

from backend.app.desktop.capabilities.evolution_strategies import freeze_strategies, strategy_catalog
from backend.app.desktop.capabilities.strategy_snapshots import PatrolStrategyReadRequest, read_strategy_page

import asyncio
import uuid
from pathlib import Path
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.patrol.agent_loop.resources.budgets import configured_provider_count
from backend.app.desktop.patrol.agent_loop.context_recovery import (
    ContextRecoveryOpportunityService,
)
from backend.app.desktop.patrol.agent_loop.governance.intervention_lifecycle import (
    InterventionLifecycleRepository,
)
from backend.app.desktop.patrol.agent_loop.journal.journal_models import LoopJournalSequence
from backend.app.desktop.patrol.agent_loop.missions.mission_models import LoopMissionRevision
from backend.app.desktop.patrol.agent_loop.missions.mission_projection import EffectiveMissionProjector
from backend.app.desktop.patrol.agent_loop.models import (
    AgentLoop,
    LoopBudgetUsage,
    LoopContextMembership,
    LoopDelegationGrant,
    LoopGoalRevision,
    LoopObservation,
    LoopPendingDecision,
    LoopRound,
    LoopUserIntent,
    LoopWorkerRequest,
)
from backend.app.desktop.patrol.agent_loop.observations.builder import (
    LoopObservationBuilder,
    observation_hash,
)
from backend.app.desktop.patrol.agent_loop.rounds.lifecycle import TERMINAL_ROUND_STATUSES
from backend.app.desktop.patrol.agent_loop.schemas import LoopObservationEnvelope
from backend.app.desktop.patrol.agent_loop.queries.patrol_read_view import PatrolReadView, PatrolWorkerReadRequest, PatrolCausalReadRequest
from backend.app.desktop.patrol.agent_loop.task_progress.baseline import legacy_baseline
from backend.app.desktop.patrol.agent_loop.task_progress.consolidation import initial_progress
from backend.app.desktop.patrol.agent_loop.task_progress.contracts import (
    RoundDecisionInputs,
    TaskProgressDocument,
    canonical_hash,
)
from backend.app.desktop.patrol.agent_loop.task_progress.repository import (
    TaskProgressRepository,
)
from backend.app.desktop.patrol.agent_loop.task_progress.source_reader import (
    TaskDeltaSourceReader,
)
from backend.app.desktop.contexts.evolution import (
    ContextRevisionReader,
    ContextRevisionRepository,
)
from backend.app.desktop.contexts.evolution.committed_lineage import (
    CommittedLineageReader,
)
from backend.app.desktop.persistence.models import DesktopRun, DesktopThread
from backend.app.desktop.workspaces.coordination.models import WorkspaceSlot


class ObservationCaptureSuperseded(RuntimeError):
    pass


class PatrolReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    context_id: str
    revision_id: str
    view: str = Field(default="display", pattern=r"^(display|execution)$")
    max_messages: int = Field(default=32, ge=1, le=64)


class LoopObservationService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], checkpointer
    ) -> None:
        self._sessions = sessions
        self._builder = LoopObservationBuilder()
        self._revisions = ContextRevisionRepository()
        self._reader = ContextRevisionReader(self._revisions, checkpointer)
        self._interventions = InterventionLifecycleRepository()
        self._progress = TaskProgressRepository()
        self._sources = TaskDeltaSourceReader()
        self._lineage = CommittedLineageReader()

    async def capture(self, loop_id: str, round_id: str) -> LoopObservationEnvelope:
        for attempt in range(3):
            async with self._sessions() as session:
                round_row = await session.get(LoopRound, round_id)
                if round_row is None or round_row.loop_id != loop_id:
                    raise LookupError("Loop 或 round 不存在")
                existing = await session.get(LoopObservation, round_row.observation_id) if round_row.observation_id else None
                refresh = existing is not None and await self._bootstrap_changed(session, existing)
                if existing is not None and not refresh:
                    return LoopObservationEnvelope.model_validate(existing.envelope)
                if round_row.trigger_kind != "user_input":
                    await self._progress.require_ready(session, loop_id)
            views = await self._prepare_views(loop_id)
            try:
                return await self._capture_once(loop_id, round_id, views, refresh_id=existing.observation_id if refresh else None)
            except _PreparedViewsChanged:
                if attempt == 2:
                    raise
            except DBAPIError as exc:
                state = getattr(exc.orig, "sqlstate", None) or getattr(
                    exc.orig, "pgcode", None
                )
                if state not in {"40001", "40P01"} or attempt == 2:
                    raise
            await asyncio.sleep(0.02 * (attempt + 1))
        raise RuntimeError("Observation 冻结重试耗尽")

    async def refresh(self, loop_id: str, round_id: str) -> LoopObservationEnvelope:
        for attempt in range(3):
            async with self._sessions() as session:
                round_row = await session.get(LoopRound, round_id)
                if round_row is None or round_row.loop_id != loop_id or round_row.decision_id:
                    raise ObservationCaptureSuperseded("observation_authority_superseded")
                identity = round_row.observation_id
            views = await self._prepare_views(loop_id)
            try:
                return await self._capture_once(loop_id, round_id, views, refresh_id=identity)
            except _PreparedViewsChanged:
                if attempt == 2:
                    raise
            except DBAPIError as exc:
                state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
                if state not in {"40001", "40P01"} or attempt == 2:
                    raise
            await asyncio.sleep(0.02 * (attempt + 1))
        raise RuntimeError("Observation 更新重试耗尽")

    async def _capture_once(
        self, loop_id: str, round_id: str, views: dict, *, refresh_id=None
    ) -> LoopObservationEnvelope:
        async with self._sessions.begin() as session:
            await session.connection(
                execution_options={"isolation_level": "REPEATABLE READ"}
            )
            loop = await session.get(AgentLoop, loop_id, with_for_update=True)
            round_row = await session.get(LoopRound, round_id, with_for_update=True)
            if loop is None or round_row is None or round_row.loop_id != loop_id:
                raise LookupError("Loop 或 round 不存在")
            existing = await session.get(LoopObservation, round_row.observation_id) if round_row.observation_id else None
            if existing is not None and existing.observation_id != refresh_id:
                return LoopObservationEnvelope.model_validate(existing.envelope)
            if refresh_id and round_row.decision_id is None:
                round_row.authority_revision, round_row.goal_revision = loop.authority_revision, loop.goal_revision
                round_row.status = "observed"
            if (
                loop.status != "running"
                or round_row.status in TERMINAL_ROUND_STATUSES
                or round_row.authority_revision != loop.authority_revision
                or round_row.goal_revision != loop.goal_revision
            ):
                raise ObservationCaptureSuperseded("observation_authority_superseded")
            if round_row.trigger_kind != "user_input":
                await self._progress.require_ready(session, loop_id)
            if refresh_id:
                from backend.app.desktop.patrol.agent_loop.rounds.lifecycle import current_frontier_hash

                round_row.frontier_hash = await current_frontier_hash(session, loop_id) or round_row.frontier_hash
                slot = await session.scalar(select(WorkspaceSlot).where(WorkspaceSlot.workspace_id == loop.workspace_id,
                    WorkspaceSlot.kind == "authoritative", WorkspaceSlot.lifecycle == "active"))
                if slot is not None:
                    round_row.workspace_revision = slot.revision
            envelope = await self.prepare_state(session, loop, round_row, views)
            envelope = await self.freeze_version(session, loop, round_row, envelope)
            from backend.app.desktop.patrol.agent_loop.models import LoopEventOutbox

            preemption = await session.scalar(select(LoopEventOutbox.payload).where(
                LoopEventOutbox.idempotency_key == f"cognition-preempted:{envelope.observation_id}"))
        if preemption is not None:
            from backend.app.desktop.patrol.agent_loop.decisions.cognition import notify_preemption

            await notify_preemption(preemption)
        return envelope

    @staticmethod
    async def _bootstrap_changed(session, observation):
        round_row = await session.get(LoopRound, observation.round_id)
        if round_row.decision_id is not None:
            return False
        for item in observation.envelope.get("portfolio_frontier", ()):
            if item.get("revision_id") is None:
                context = await session.get(DesktopThread, item["context_id"])
                if context is not None and context.current_revision_id is not None:
                    return True
        return False

    async def prepare_views(self, loop_id: str) -> dict:
        return await self._prepare_views(loop_id)

    async def prepare_state(self, session, loop, round_row, views, *, visible_context=None, intake=False):
        envelope = await self._build(session, loop, round_row, views, intake=intake)
        manifest = await self._sources.capture(session, loop, boundary=uuid.uuid4().hex)
        if not manifest.complete:
            raise ValueError(manifest.blocker)
        previous = await self._progress.current(session, loop.loop_id)
        if previous is None:
            legacy = await session.scalar(
                select(LoopObservation.observation_id)
                .where(LoopObservation.loop_id == loop.loop_id)
                .limit(1)
            )
            document = initial_progress(
                envelope.mission or {},
                loop.goal_revision,
                migration=legacy is not None,
            )
            baseline_keys = ()
            if legacy is not None:
                document = legacy_baseline(
                    envelope.mission or {}, loop.goal_revision, manifest
                )
                baseline_keys = tuple(
                    source.source_key for source in manifest.sources
                )
            previous = await self._progress.initialize(
                session, loop.loop_id, document, source_keys=baseline_keys
            )
            if legacy is not None:
                manifest = manifest.model_copy(update={"sources": ()})
        context_ids = tuple(
            (
                await session.scalars(
                    select(LoopContextMembership.context_id).where(
                        LoopContextMembership.loop_id == loop.loop_id
                    )
                )
            ).all()
        )
        roots = await self._lineage.published_roots(session, context_ids)
        lineage = await self._lineage.snapshot(
            session, roots, workspace_id=loop.workspace_id
        )
        from backend.app.desktop.patrol.agent_loop.projections.causal_snapshot import CausalSnapshotReader

        intervention = await CausalSnapshotReader().capture(session, loop, visible_context)
        envelope = envelope.model_copy(
            update={
                "input_schema_version": 1,
                "decision_inputs_ref": manifest.boundary,
                "previous_task_progress": previous.document,
                "task_delta": manifest.model_dump(mode="json"),
                "committed_lineage": lineage,
                "intervention": intervention,
            }
        )
        return envelope

    async def freeze_version(self, session, loop, round_row, envelope, *, intent=None):
        observation_id = uuid.uuid4().hex
        prior_id = round_row.observation_id
        generation = int(await session.scalar(select(func.max(LoopObservation.generation)).where(
            LoopObservation.round_id == round_row.round_id)) or 0) + 1
        previous = await self._progress.current(session, loop.loop_id)
        if previous is None:
            raise RuntimeError("Observation 缺少已发布 Task Progress")
        pre_state = None
        pre_hash = None
        if intent is not None:
            pre_state = envelope.model_dump(mode="json", exclude={"observation_id", "generation", "pre_input_state", "pre_input_hash", "causal_chain", "decision_inputs_ref"})
            pre_hash = canonical_hash(pre_state)
            intent.introduced_observation_id = observation_id
            intent.introduced_generation = generation
            intent.snapshot_hash = pre_hash
            intent.observed_round_id = round_row.round_id
            intent.request_payload = {**intent.request_payload, "accepted_sequence": envelope.projection_sequence,
                "accepted_at": (intent.created_at or datetime.now(UTC)).isoformat()}
        intents = await self._observe_intents(session, loop.loop_id, round_row.round_id, mark=True)
        if intent is not None:
            from backend.app.desktop.patrol.agent_loop.projections.causal_read_view import CausalInputReadView

            prior_chain = await self._causal_chain(session, [row for row in intents if row.intent_id != intent.intent_id])
            read_view = await CausalInputReadView.capture(session, loop.loop_id, pre_state,
                intent.request_payload.get("request", {}), previous_source=prior_chain[-1] if prior_chain else None)
            intent.request_payload = {**intent.request_payload, "causal_read_view": read_view}
        chain = await self._causal_chain(session, intents)
        manifest = await self._sources.capture(session, loop, boundary=observation_id)
        if not manifest.complete:
            raise ValueError(manifest.blocker)
        envelope = envelope.model_copy(update={"observation_id": observation_id, "generation": generation,
            "decision_inputs_ref": observation_id, "pre_input_state": pre_state, "pre_input_hash": pre_hash,
            "causal_chain": chain, "user_intents": tuple(self._intent_view(row) for row in intents),
            "task_delta": manifest.model_dump(mode="json")})
        lineage = envelope.committed_lineage
        row = LoopObservation(
            observation_id=observation_id,
            loop_id=loop.loop_id,
            round_id=round_row.round_id,
            generation=generation,
            envelope=envelope.model_dump(mode="json"),
            envelope_hash=observation_hash(envelope),
            projection_sequence=envelope.projection_sequence,
            base_entity_revisions=envelope.base_entity_revisions,
        )
        session.add(row)
        await session.flush()
        inputs = RoundDecisionInputs(
            round_id=round_row.round_id,
            observation_id=observation_id,
            observation_hash=row.envelope_hash,
            previous_progress_id=previous.progress_id,
            previous_progress_hash=previous.content_hash,
            previous_progress=TaskProgressDocument.model_validate(
                previous.document
            ),
            task_delta=manifest,
            manifest_hash=canonical_hash(manifest),
            lineage=lineage,
            topology_hash=lineage["topology_hash"],
        )
        await self._progress.freeze(session, loop.loop_id, inputs)
        round_row.observation_id = row.observation_id
        round_row.barrier = {key: value for key, value in (round_row.barrier or {}).items() if key != "refresh_observation"}
        if intent is not None or prior_id is not None:
            from backend.app.desktop.patrol.agent_loop.decisions.cognition import preempt_observation

            await preempt_observation(session, loop, round_row, prior_id, observation_id, generation)
        from backend.app.desktop.patrol.agent_loop.journal.event_contract import CanonicalEventDraft
        from backend.app.desktop.patrol.agent_loop.journal.event_journal import LoopEventJournal

        await LoopEventJournal().append(session, loop.loop_id, CanonicalEventDraft(
            kind="observation.frozen", entity_type="observation", entity_id=observation_id,
            entity_revision=1, correlation_id=round_row.round_id,
            payload={"observation_id": observation_id, "round_id": round_row.round_id,
                     "generation": generation, "observation_hash": row.envelope_hash,
                     "frozen_at": row.created_at.isoformat()},
            idempotency_key=f"observation-frozen:{observation_id}"))
        from backend.app.desktop.patrol.agent_loop.journal.round_events import RoundStateEventRecorder

        await RoundStateEventRecorder().record(session, round_row)
        if loop.status == "running" and loop.health != "preparing":
            loop.health = "deciding"
        if intent is None and loop.waiting_reason and loop.waiting_reason.startswith(
            "progress_memory_"
        ):
            loop.waiting_reason = None
        return envelope

    @staticmethod
    async def _causal_chain(session, intents):
        return tuple({"intent_id": row.intent_id, "submission_id": row.request_payload.get("request", {}).get("submission_id"),
            "origin_loop_id": row.loop_id,
            "availability": "available" if row.introduced_observation_id else "legacy_unknown",
            "observation_id": row.introduced_observation_id, "generation": row.introduced_generation,
            "snapshot_hash": row.snapshot_hash, "accepted_at": row.request_payload.get("accepted_at"),
            "accepted_sequence": row.request_payload.get("accepted_sequence"),
            **({"read_view": row.request_payload["causal_read_view"]} if row.request_payload.get("causal_read_view") else {}),
            "input": {"content": row.content, **{key: row.request_payload.get("request", {}).get(key)
                  for key in ("input_type", "request_id", "request_revision", "file_refs")},
                  **({"attachments": row.request_payload["attachments"]} if row.request_payload.get("attachments") else {})}}
            for row in sorted(intents, key=lambda row: (row.created_at, row.request_payload.get("accepted_sequence", -1), row.intent_id))
            if row.intent_kind == "workspace_input")

    @staticmethod
    def _intent_view(row):
        request = (row.request_payload or {}).get("request") or {}
        return {"intent_id": row.intent_id, "intent_kind": row.intent_kind,
            "request_hash": row.request_payload.get("request_hash"), "input_type": request.get("input_type"),
            **({"file_refs": request["file_refs"]} if request.get("file_refs") else {}),
            **({"attachments": row.request_payload["attachments"]} if row.request_payload.get("attachments") else {}),
            "request_id": request.get("request_id"), "scope": row.scope, "context_id": row.target_context_id,
            "content": row.content, "goal_revision": row.goal_revision, "authority_revision": row.authority_revision}

    async def _prepare_views(self, loop_id: str) -> dict:
        views = {}
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, loop_id)
            slot = await session.scalar(select(WorkspaceSlot).where(
                WorkspaceSlot.workspace_id == loop.workspace_id, WorkspaceSlot.kind == "authoritative",
                WorkspaceSlot.lifecycle == "active")) if loop else None
            contexts = tuple(
                (
                    await session.scalars(
                        select(DesktopThread)
                        .join(
                            LoopContextMembership,
                            LoopContextMembership.context_id == DesktopThread.task_id,
                        )
                        .where(
                            LoopContextMembership.loop_id == loop_id,
                            LoopContextMembership.status == "active",
                        )
                    )
                ).all()
            )
            for context in contexts:
                revision = await self._revisions.current(session, context.task_id)
                if revision is not None:
                    for view in ("display", "authored"):
                        views[
                            (revision.ref.revision_id, view)
                        ] = await self._reader.read(session, revision.ref, view)
        if slot is not None:
            from backend.app.desktop.workspaces.coordination.fingerprints import WorkspaceFingerprinter

            head, dirty = await asyncio.to_thread(WorkspaceFingerprinter.git_state, Path(slot.root_path))
            views["workspace"] = (slot.slot_id, slot.revision, slot.current_fingerprint, head, dirty)
        return views

    async def _build(
        self, session: AsyncSession, loop: AgentLoop, round_row: LoopRound, views: dict, *, intake=False
    ) -> LoopObservationEnvelope:
        mission_contract, grant = await self._authority(session, loop, allow_inactive=intake)
        memberships = list(
            (
                await session.scalars(
                    select(LoopContextMembership)
                    .where(
                        LoopContextMembership.loop_id == loop.loop_id,
                        LoopContextMembership.status == "active",
                    )
                    .order_by(LoopContextMembership.created_at)
                )
            ).all()
        )
        frontier = await self._frontier(session, loop, memberships, views)
        from backend.app.desktop.patrol.agent_loop.context_expansion.models import LoopContextExpansion
        from backend.app.desktop.patrol.agent_loop.context_expansion.repository import ContextExpansionRepository

        expansions = await session.scalars(select(LoopContextExpansion).where(
            LoopContextExpansion.loop_id == loop.loop_id, LoopContextExpansion.level == "required"))
        obligations = tuple({"opportunity_id": item.opportunity_id, "round_id": item.round_id,
            "source_frontier": item.source_frontier} for item in expansions
            if not ContextExpansionRepository.is_terminal(item.state))
        runs, pending, workers = await self._recent_activity(session, loop.loop_id)
        usage = await session.get(LoopBudgetUsage, loop.loop_id)
        user_intents = await self._observe_intents(
            session, loop.loop_id, round_row.round_id, mark=False
        )
        slot = await session.scalar(
            select(WorkspaceSlot).where(
                WorkspaceSlot.workspace_id == loop.workspace_id,
                WorkspaceSlot.kind == "authoritative",
                WorkspaceSlot.lifecycle != "deleted",
            )
        )
        prepared_workspace = views.get("workspace")
        if intake and slot and prepared_workspace is None:
            prepared_workspace = (slot.slot_id, slot.revision, slot.current_fingerprint, slot.base_revision, None)
        if slot and (not prepared_workspace or prepared_workspace[:3] !=
                     (slot.slot_id, slot.revision, slot.current_fingerprint)):
            raise _PreparedViewsChanged("工作区在准备后变化，重新冻结")
        isolated_slots = (await session.scalars(select(WorkspaceSlot).where(
            WorkspaceSlot.workspace_id == loop.workspace_id, WorkspaceSlot.owner_loop_id == loop.loop_id,
            WorkspaceSlot.kind == "isolated", WorkspaceSlot.lifecycle.in_(("active", "retained"))
        ).order_by(WorkspaceSlot.created_at, WorkspaceSlot.slot_id))).all()
        from backend.app.desktop.workspaces.coordination.models import WorkspaceAdoption

        all_adoptions = (await session.scalars(select(WorkspaceAdoption).where(
            WorkspaceAdoption.source_slot_id.in_([item.slot_id for item in isolated_slots])
        ).order_by(WorkspaceAdoption.created_at.desc()))).all()
        adoptions = all_adoptions[:24]
        from backend.app.desktop.patrol.agent_loop.decisions.evidence import workspace_admission_facts

        adoption_facts = await workspace_admission_facts(session, loop.loop_id, isolated_slots, slot, all_adoptions)
        from backend.app.desktop.patrol.agent_loop.workspaces.workspace_repair import WorkspaceConflictRepair
        recovery = await ContextRecoveryOpportunityService(
            _PreparedContextReader(views)
        ).discover(
            session,
            loop=loop,
            round_row=round_row,
            grant=grant,
            workspace_revision=slot.revision if slot else round_row.workspace_revision,
            frontier=frontier,
            runs=tuple(runs),
        )
        journal_sequence = await session.get(LoopJournalSequence, loop.loop_id)
        from backend.app.desktop.patrol.agent_loop.completion.completion_admission import CompletionRequestAdmission

        completion_admission = await CompletionRequestAdmission().read(session, loop, round_row)
        from backend.app.desktop.patrol.agent_loop.completion.completion_eligibility import CompletionEligibilityPolicy

        completion_eligibility = await CompletionEligibilityPolicy().read(session, loop, round_row)
        base_entity_revisions = {
            "loop": loop.revision,
            "mission": loop.goal_revision,
            "grant": loop.authority_revision,
            "workspace": slot.revision if slot else round_row.workspace_revision,
            **{
                f"context:{item['context_id']}": str(item.get("revision_id") or "")
                for item in frontier
            },
        }
        return self._builder.build(
            interaction_mode=loop.interaction_mode,
            information_requests=await self._information_requests(session, loop.loop_id),
            loop_id=loop.loop_id,
            loop_revision=loop.revision,
            round_id=round_row.round_id,
            goal_revision=loop.goal_revision,
            authority_revision=loop.authority_revision,
            observed_frontier_hash=round_row.frontier_hash,
            projection_sequence=int(
                journal_sequence.last_sequence if journal_sequence is not None else 0
            ),
            base_entity_revisions=base_entity_revisions,
            mission=mission_contract,
            evolution_strategies=await freeze_strategies(session, loop.workspace_id),
            completion_admission=completion_admission,
            completion_eligibility=completion_eligibility,
            grant={**self._grant_view(grant), "equipment": {
                key: (loop.equipment or {}).get(key) for key in ("permissions", "access_mode")}},
            portfolio_frontier=frontier,
            stable_results=self._run_views(runs),
            workspace={
                "slot_id": slot.slot_id if slot else None,
                "revision": slot.revision if slot else round_row.workspace_revision,
                "fingerprint": slot.current_fingerprint if slot else None,
                "git_revision": prepared_workspace[3] if prepared_workspace else None,
                "git_dirty": prepared_workspace[4] if prepared_workspace else None,
                "isolation_authorized": "isolate_workspace" in (grant.capabilities or []),
                "isolated_slots": tuple({"slot_id": item.slot_id, "revision": item.revision,
                    "base_revision": item.base_revision, "fingerprint": item.current_fingerprint,
                    "lane_id": item.owner_lane_id, "lifecycle": item.lifecycle,
                    "adoption_admission": adoption_facts[item.slot_id],
                    "repair": (item.metadata_json or {}).get("repair")} for item in isolated_slots),
                "adoptions": tuple({"adoption_id": item.adoption_id, "source_slot_id": item.source_slot_id,
                    "source_revision": item.source_revision, "status": item.status,
                    "expected_target_revision": item.expected_target_revision,
                    "resulting_target_revision": item.resulting_target_revision, "conflict": item.conflict,
                    "repair_context": WorkspaceConflictRepair.reference((item.evidence or {}).get("repair_context"))}
                    for item in adoptions),
            },
            budget={
                "limits": grant.budgets,
                "usage": self._usage(usage, loop, len(memberships)),
                "progress_model_name": (loop.equipment or {}).get("patrol_model_name")
                or (loop.equipment or {}).get("model_name"),
            },
            pending_decisions=tuple(
                {
                    "pending_decision_id": row.pending_decision_id,
                    "kind": row.kind,
                    "delegable": row.delegable,
                    "status": row.status,
                    "payload": row.payload,
                }
                for row in pending
            ),
            worker_results=tuple(
                {
                    "request_id": row.worker_request_id,
                    "round_id": row.round_id,
                    "kind": row.kind,
                    "status": row.status,
                    "result": row.result,
                }
                for row in workers
            ),
            user_intents=tuple(
                {
                    "intent_id": row.intent_id,
                    "intent_kind": row.intent_kind,
                    "request_hash": (row.request_payload or {}).get("request_hash"),
                    "input_type": ((row.request_payload or {}).get("request") or {}).get("input_type"),
                    "request_id": ((row.request_payload or {}).get("request") or {}).get("request_id"),
                    **({"file_refs": row.request_payload["request"]["file_refs"]} if (row.request_payload or {}).get("request", {}).get("file_refs") else {}),
                    **({"attachments": row.request_payload["attachments"]} if row.request_payload.get("attachments") else {}),
                    "scope": row.scope,
                    "context_id": row.target_context_id,
                    "content": row.content,
                    "goal_revision": row.goal_revision,
                    "authority_revision": row.authority_revision,
                }
                for row in user_intents
            ),
            expansion_handles=tuple(
                {"context_id": item["context_id"], "revision_id": item["revision_id"]}
                for item in frontier
                if item.get("revision_id")
            ),
            expansion_obligations=obligations,
            recovery_opportunities=recovery.opportunities,
            recovery_waiting_reason=recovery.waiting_reason,
        )

    async def _authority(self, session: AsyncSession, loop: AgentLoop, *, allow_inactive=False):
        mission = await session.scalar(
            select(LoopMissionRevision).where(
                LoopMissionRevision.loop_id == loop.loop_id,
                LoopMissionRevision.revision == loop.goal_revision,
            )
        )
        goal = (
            None
            if mission is not None
            else await session.scalar(
                select(LoopGoalRevision).where(
                    LoopGoalRevision.loop_id == loop.loop_id,
                    LoopGoalRevision.revision == loop.goal_revision,
                )
            )
        )
        grant = await session.scalar(
            select(LoopDelegationGrant).where(
                LoopDelegationGrant.loop_id == loop.loop_id,
                LoopDelegationGrant.revision == loop.authority_revision,
            )
        )
        if mission is None and goal is None or grant is None:
            raise RuntimeError("Loop 缺少当前 Mission 或 grant")
        if not allow_inactive and (grant.status != "active" or (
            grant.expires_at is not None and grant.expires_at <= datetime.now(UTC)
        )):
            raise ObservationCaptureSuperseded("observation_authority_superseded")
        mission_contract = EffectiveMissionProjector.from_rows(
            structured=mission, legacy=goal
        ).model_payload()
        return mission_contract, grant

    async def _recent_activity(self, session: AsyncSession, loop_id: str):
        runs = list(
            (
                await session.scalars(
                    select(DesktopRun)
                    .where(DesktopRun.loop_id == loop_id)
                    .order_by(DesktopRun.created_at.desc())
                    .limit(24)
                )
            ).all()
        )
        pending = list(
            (
                await session.scalars(
                    select(LoopPendingDecision).where(
                        LoopPendingDecision.loop_id == loop_id,
                        LoopPendingDecision.status == "pending",
                    )
                )
            ).all()
        )
        workers = list(
            (
                await session.scalars(
                    select(LoopWorkerRequest)
                    .where(
                        LoopWorkerRequest.loop_id == loop_id,
                        LoopWorkerRequest.status.in_(["success", "error"]),
                    )
                    .order_by(LoopWorkerRequest.created_at.desc())
                )
            ).all()
        )
        return runs, pending, workers

    async def _observe_intents(
        self, session: AsyncSession, loop_id: str, round_id: str, *, mark=True
    ):
        loop = await session.get(AgentLoop, loop_id)
        workspace_mode = loop.interaction_mode == "workspace_patrol"
        from backend.app.desktop.patrol.agent_loop.inputs.patrol_inputs import workspace_input_scope
        user_intents = list(
            (
                await session.scalars(
                    select(LoopUserIntent)
                    .where(
                        workspace_input_scope(loop) if workspace_mode else LoopUserIntent.loop_id == loop_id,
                        ((LoopUserIntent.status == "pending") | ((LoopUserIntent.intent_kind.in_(("direct_message", "workspace_input"))) &
                         (LoopUserIntent.status == "observed") & (LoopUserIntent.delivery_state == "observed"))),
                    )
                    .order_by(LoopUserIntent.created_at, LoopUserIntent.intent_id)
                    .limit(4097 if workspace_mode else 32)
                )
            ).all()
        )
        if workspace_mode and len(user_intents) > 4096:
            raise ValueError("patrol_input_budget_exceeded: 待处理输入未完整冻结")
        for intent in user_intents:
            if not mark:
                continue
            intent.status = "observed"
            if intent.intent_kind == "workspace_input" or intent.observed_round_id is None:
                intent.observed_round_id = round_id
            if intent.delivery_state == "accepted":
                await self._interventions.transition(
                    session, intent.intent_id, "observed"
                )
        return user_intents

    @staticmethod
    async def _information_requests(session, loop_id):
        from backend.app.desktop.patrol.agent_loop.waits.wait_models import LoopWaitRequest, LoopWaitResponse
        rows = tuple(await session.scalars(select(LoopWaitRequest).where(LoopWaitRequest.loop_id == loop_id,
            LoopWaitRequest.kind == "clarification", LoopWaitRequest.status == "open",
            LoopWaitRequest.scope["information_only"].astext == "true")))
        result = []
        for row in rows:
            answer = await session.scalar(select(LoopWaitResponse).where(LoopWaitResponse.request_id == row.request_id))
            result.append({"request_id": row.request_id, "revision": row.revision, "prompt": row.prompt,
                           "scope": row.scope, "answered": answer is not None,
                           "response": None if answer is None else {"response_id": answer.response_id,
                               "submission_id": answer.answer.get("submission_id"), "text": answer.answer.get("text")}})
        return tuple(result)

    @staticmethod
    def _grant_view(grant: LoopDelegationGrant) -> dict:
        return {
            "grant_id": grant.grant_id,
            "holder_id": grant.holder_id,
            "revision": grant.revision,
            "capabilities": grant.capabilities,
            "context_scope": grant.context_scope,
            "permission_scope": grant.permission_scope,
            "delegable_gates": grant.delegable_gates,
            "compression_policy": grant.compression_policy,
            "budgets": grant.budgets,
            "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
        }

    @staticmethod
    def _run_views(runs: list[DesktopRun]) -> tuple[dict, ...]:
        return tuple(
            {
                "run_id": row.run_id,
                "context_id": row.task_id,
                "status": row.status,
                "error": row.error,
                "final_checkpoint_id": row.final_checkpoint_id,
                "workspace_result": row.workspace_result,
                "workspace_anchor": row.workspace_anchor,
            }
            for row in runs
        )

    async def _frontier(
        self,
        session: AsyncSession,
        loop: AgentLoop,
        memberships: list[LoopContextMembership],
        views: dict,
    ) -> tuple[dict[str, Any], ...]:
        result: list[dict[str, Any]] = []
        for membership in memberships:
            context = await session.get(DesktopThread, membership.context_id)
            revision = (
                await self._revisions.current(session, membership.context_id)
                if context
                else None
            )
            messages = self._message_preview(views, revision.ref) if revision else ()
            from backend.app.desktop.patrol.agent_loop.execution.directive_equipment import resolve_context_equipment
            from backend.app.desktop.contexts.curation.models import CurationLane

            lane = await session.get(CurationLane, membership.lane_id) if membership.lane_id else None
            try:
                equipment = await resolve_context_equipment(session, loop, membership.context_id)
            except ValueError:
                equipment = {"permissions": [], "access_mode": "read-only"}
            result.append(
                {
                    "membership_id": membership.membership_id,
                    "context_id": membership.context_id,
                    "lane_id": membership.lane_id,
                    "role": membership.role,
                    "lane_policy": {"workspace_mode": (lane.lane_policy or {}).get("workspace_mode")} if lane else {},
                    "equipment": {key: equipment.get(key) for key in ("permissions", "access_mode")},
                    "required_barrier": membership.required_barrier,
                    "revision": revision.ref.model_dump(mode="json")
                    if revision
                    else None,
                    "revision_id": revision.ref.revision_id if revision else None,
                    "generation": revision.ref.generation if revision else None,
                    "checkpoint_id": revision.ref.checkpoint_id if revision else None,
                    "projection_status": revision.projection_status.value
                    if revision
                    else "legacy",
                    "content_hash": revision.content_hash if revision else None,
                    "message_evidence_preview": messages,
                }
            )
        return tuple(result)

    def _message_preview(self, views: dict, ref) -> tuple[dict[str, Any], ...]:
        view = views.get((ref.revision_id, "display"))
        if view is None or view.ref != ref:
            raise _PreparedViewsChanged("Context frontier 在准备后变化，重新冻结")
        return tuple(
            {
                "message_id": str(message.get("id")),
                "role": message.get("role"),
                "content": self._preview_content(message.get("content", "")),
                "tool_calls": message.get("tool_calls") or [],
                "tool_call_id": message.get("tool_call_id"),
                "name": message.get("name"),
                "status": message.get("status"),
            }
            for message in view.messages[-12:]
            if message.get("id")
        )

    async def selective_read(
        self,
        observation: LoopObservationEnvelope,
        requests: tuple[PatrolReadRequest | PatrolWorkerReadRequest | PatrolCausalReadRequest | PatrolStrategyReadRequest, ...],
    ) -> tuple[dict[str, Any], ...]:
        allowed = {
            (str(item.get("context_id")), str(item.get("revision_id"))): item.get(
                "revision"
            )
            for item in observation.portfolio_frontier
            if item.get("revision")
        }
        results: list[dict[str, Any]] = []
        async with self._sessions() as session:
            for request in requests:
                if isinstance(request, PatrolStrategyReadRequest):
                    results.append(await self._strategy_page(session, observation, request))
                    continue
                if isinstance(request, PatrolCausalReadRequest):
                    from backend.app.desktop.patrol.agent_loop.projections.causal_snapshot import read_causal_snapshot

                    entry = next((item for item in observation.causal_chain
                        if item["intent_id"] == request.intent_id and item["snapshot_hash"] == request.snapshot_hash), None)
                    if entry is None:
                        raise ValueError("Patrol 因果读取超出冻结 Observation handles")
                    snapshot = await read_causal_snapshot(session, observation.loop_id, entry)
                    results.append(PatrolReadView.causal_page(snapshot, request))
                    continue
                if isinstance(request, PatrolWorkerReadRequest):
                    results.append(await self._worker_page(session, observation, request))
                    continue
                payload = allowed.get((request.context_id, request.revision_id))
                if payload is None:
                    raise ValueError(
                        "Patrol selective read 超出冻结 observation handles"
                    )
                ref = self._ref(payload)
                view = await self._reader.read(session, ref, request.view)
                messages = tuple(view.messages[-request.max_messages :])
                results.append(
                    {
                        "context_id": request.context_id,
                        "revision": payload,
                        "view": request.view,
                        "messages": self._bounded_messages(messages),
                    }
                )
        return tuple(results)

    async def _strategy_page(self, session, observation, request):
        loop = await session.get(AgentLoop, observation.loop_id)
        round_row = await session.get(LoopRound, observation.round_id)
        identity = observation.observation_id or observation.decision_inputs_ref
        if identity is None and round_row is not None and any("strategy" in item for item in observation.evolution_strategies):
            identity = round_row.observation_id  # Historical full envelopes predate explicit identity fields.
        if (loop is None or round_row is None or round_row.loop_id != loop.loop_id
                or loop.status != "running" or round_row.status in TERMINAL_ROUND_STATUSES
                or loop.authority_revision != observation.authority_revision
                or round_row.authority_revision != observation.authority_revision
                or loop.goal_revision != observation.goal_revision
                or round_row.observation_id != identity):
            raise ValueError("策略读取授权或 Observation 已失效")
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == observation.authority_revision))
        if grant is None or grant.status != "active" or (grant.expires_at and grant.expires_at <= datetime.now(UTC)):
            raise ValueError("策略读取授权已撤销或过期")
        base = await session.get(LoopObservation, identity)
        if (base is None or base.loop_id != loop.loop_id or base.round_id != round_row.round_id
                or (observation.generation and base.generation != observation.generation)
                or observation_hash(LoopObservationEnvelope.model_validate(base.envelope)) != base.envelope_hash
                or strategy_catalog(base.envelope.get("evolution_strategies", ())) != strategy_catalog(observation.evolution_strategies)):
            raise ValueError("策略读取冻结 Observation identity/hash 不匹配")
        return await read_strategy_page(session, base.envelope.get("evolution_strategies", ()), request)

    async def worker_sources(self, observation) -> tuple[dict, ...]:
        async with self._sessions() as session:
            return await self._frozen_worker_sources(session, observation)

    async def causal_sources(self, observation) -> tuple[dict, ...]:
        from backend.app.desktop.patrol.agent_loop.projections.causal_snapshot import read_causal_snapshot

        async with self._sessions() as session:
            return tuple([{**entry, "snapshot": await read_causal_snapshot(session, observation.loop_id, entry)}
                for entry in observation.causal_chain])

    async def _frozen_worker_sources(self, session, observation) -> tuple[dict, ...]:
        from backend.app.desktop.patrol.agent_loop.decisions.decision_context import DecisionSupplementRepository

        base = await session.get(LoopObservation, observation.observation_id or observation.decision_inputs_ref)
        if base is None:
            raise ValueError("Patrol Worker 来源缺少冻结 Observation")
        if observation_hash(LoopObservationEnvelope.model_validate(base.envelope)) != base.envelope_hash:
            raise ValueError("Patrol Worker 冻结 Observation hash 不匹配")
        supplement = await DecisionSupplementRepository().curator_results(session, observation)
        sources = {(item["request_id"], canonical_hash(item.get("result") or {})): item for item in base.envelope.get("worker_results", ())}
        for item in supplement:
            sources[(item["request_id"], canonical_hash(item.get("result") or {}))] = item
        identities = tuple({identity for identity, _ in sources})
        rounds = dict((await session.execute(select(LoopWorkerRequest.worker_request_id, LoopWorkerRequest.round_id).where(
            LoopWorkerRequest.loop_id == observation.loop_id, LoopWorkerRequest.worker_request_id.in_(identities)))).all()) if identities else {}
        return tuple({**item, "round_id": item.get("round_id") or rounds.get(identity)} for (identity, _), item in sources.items())

    async def _worker_page(self, session, observation, request) -> dict:
        loop = await session.get(AgentLoop, observation.loop_id)
        round_row = await session.get(LoopRound, observation.round_id)
        if (loop is None or round_row is None or round_row.observation_id != (observation.observation_id or observation.decision_inputs_ref)
                or loop.authority_revision != observation.authority_revision):
            raise ValueError("Patrol Worker 来源授权已失效")
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == observation.authority_revision))
        if grant is None or grant.status != "active" or (grant.expires_at is not None and grant.expires_at <= datetime.now(UTC)):
            raise ValueError("Patrol Worker 来源授权已失效")
        sources = await self._frozen_worker_sources(session, observation)
        source = next((item for item in sources if item["request_id"] == request.request_id and canonical_hash(item.get("result") or {}) == request.result_hash), None)
        worker = await session.get(LoopWorkerRequest, request.request_id)
        if worker is None or worker.loop_id != observation.loop_id or not any(item["request_id"] == request.request_id for item in sources):
            raise ValueError("Patrol Worker 来源超出冻结领域")
        if source is None:
            raise ValueError("Patrol Worker 来源 hash 已改变或未冻结")
        if source.get("round_id") is not None and source["round_id"] != worker.round_id:
            raise ValueError("Patrol Worker 所属轮次不匹配")
        if canonical_hash(worker.result or {}) != request.result_hash:
            raise ValueError("Patrol Worker 来源已改变")
        return PatrolReadView.page({**source, "round_id": worker.round_id}, request)

    @staticmethod
    def _ref(payload):
        from backend.app.desktop.contexts.evolution import ContextRevisionRef

        return ContextRevisionRef.model_validate(payload)

    @classmethod
    def _bounded_messages(cls, messages) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                **message,
                "content": cls._preview_content(message.get("content", "")),
            }
            for message in messages
        )

    @staticmethod
    def _preview_content(content):
        if isinstance(content, str):
            return content[:1200]
        if isinstance(content, list):
            return content[:12]
        return str(content)[:1200]

    @staticmethod
    def _usage(
        usage: LoopBudgetUsage | None, loop: AgentLoop, context_count: int
    ) -> dict[str, int]:
        fields = (
            "rounds",
            "model_calls",
            "input_tokens",
            "output_tokens",
            "retries",
            "lanes",
            "no_progress_count",
        )
        values = {field: int(getattr(usage, field, 0) or 0) for field in fields}
        created_at = loop.created_at
        now = datetime.now(UTC)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        values["duration_seconds"] = max(0, int((now - created_at).total_seconds()))
        values["providers"] = configured_provider_count(loop.equipment or {})
        values["contexts"] = context_count
        return values


class _PreparedViewsChanged(RuntimeError):
    pass


class _PreparedContextReader:
    def __init__(self, views: dict) -> None:
        self._views = views

    async def read(self, session: AsyncSession, ref, view: str):
        prepared = self._views.get((ref.revision_id, view))
        if prepared is None or prepared.ref != ref:
            raise _PreparedViewsChanged("精确 Context 内容尚未准备")
        return prepared
