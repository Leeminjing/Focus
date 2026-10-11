"""本文件对外提供 TaskProgressQueue 的决策登记、汇合登记和串行领取端口。

输入为短事务、Loop、成功 Decision 的 Observation 或已稳定的轮次集合；输出为唯一工作和冻结沉淀输入。
具体工作流为 Loop→head→最前序 work 加锁，登记完整领域边界，首次领取按当前 head 排除已吸收来源，
固定有效 Mission 及其来源后执行；重试复用原输入，blocked 前序不得被跳过；失联租约的已收费预留按 unknown 收敛。
示例：await queue.enqueue_observation(session, loop_id, observation_id)；await queue.ensure_join_ready(session, loop, round_ids)。
工作与观察使用独立身份；汇合无虚构 Observation，历史观察内容始终不变。
"""

from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import select

from backend.app.desktop.agent_loop.models import (
    AgentLoop, LoopContextMembership, LoopDecision, LoopDelegationGrant, LoopGoalRevision, LoopObservation,
)
from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.mission_projection import EffectiveMissionProjector
from backend.app.desktop.agent_loop.task_progress.contracts import (
    ConsolidationInputs, TaskDeltaManifest, TaskProgressDocument, canonical_hash,
)
from backend.app.desktop.agent_loop.task_progress.models import (
    LoopProgressHead, LoopProgressReceipt, LoopProgressWork, LoopTaskProgress,
)
from backend.app.desktop.agent_loop.task_progress.source_reader import TaskDeltaSourceReader
from backend.app.desktop.context_evolution.committed_lineage import CommittedLineageReader


class TaskProgressQueue:
    async def supersede_observation(self, session, loop_id, observation_id):
        await session.get(AgentLoop, loop_id, with_for_update=True)
        work = await session.scalar(select(LoopProgressWork).where(
            LoopProgressWork.loop_id == loop_id, LoopProgressWork.observation_id == observation_id)
            .with_for_update().execution_options(populate_existing=True))
        if work is None or work.state in {"published", "superseded", "superseding"}:
            return
        work.fence += 1
        work.state = "superseding" if any(not item.get("accounted") for item in work.usage) else "superseded"
        work.attempt_events = [*work.attempt_events, {"event": "observation_superseded", "fence": work.fence,
            "at": datetime.now(UTC).isoformat()}]

    async def enqueue_observation(self, session, loop_id, observation_id):
        loop = await session.get(AgentLoop, loop_id, with_for_update=True, populate_existing=True)
        observation = await session.get(LoopObservation, observation_id)
        decision = await session.scalar(select(LoopDecision).where(
            LoopDecision.loop_id == loop_id, LoopDecision.observation_id == observation_id,
            LoopDecision.authority_committed_at.is_not(None)))
        if observation is None or observation.loop_id != loop_id or decision is None:
            raise ValueError("progress_memory_requires_committed_observation")
        key = canonical_hash(["observation", observation_id])
        existing = await session.scalar(select(LoopProgressWork).where(
            LoopProgressWork.loop_id == loop_id, LoopProgressWork.boundary_key == key))
        if existing is not None:
            return existing
        manifest = await TaskDeltaSourceReader().capture(session, loop, boundary=key)
        return await self._enqueue(session, loop_id, key, manifest,
                                   [observation.round_id], observation_id)

    async def ensure_join_ready(self, session, loop, round_ids):
        loop = await session.get(AgentLoop, loop.loop_id, with_for_update=True, populate_existing=True)
        pending = await self._first(session, loop.loop_id)
        if pending is not None:
            return False
        manifest = await TaskDeltaSourceReader().capture(session, loop, boundary="join")
        if not manifest.complete:
            raise ValueError(manifest.blocker)
        if not manifest.sources:
            return True
        key = canonical_hash(["join", sorted(round_ids), [s.source_key for s in manifest.sources]])
        await self._enqueue(session, loop.loop_id, key,
                            manifest.model_copy(update={"boundary": key}), list(round_ids))
        return False

    async def _enqueue(self, session, loop_id, key, manifest, round_ids, observation_id=None):
        if not manifest.complete:
            raise ValueError(manifest.blocker)
        work = LoopProgressWork(work_id=uuid.uuid4().hex, observation_id=observation_id,
            loop_id=loop_id, boundary_key=key, round_ids=round_ids,
            source_manifest=manifest.model_dump(mode="json"))
        session.add(work)
        await session.flush()
        return work

    async def _first(self, session, loop_id):
        return await session.scalar(select(LoopProgressWork).where(
            LoopProgressWork.loop_id == loop_id,
            LoopProgressWork.state.not_in(("published", "superseded")))
            .order_by(LoopProgressWork.created_at, LoopProgressWork.work_id).limit(1))

    async def claim(self, session, policy, *, loop_id=None):
        now = datetime.now(UTC)
        candidates = select(AgentLoop.loop_id).where(AgentLoop.loop_id.in_(
            select(LoopProgressWork.loop_id).where(LoopProgressWork.state.in_(("pending", "claimed", "superseding")))))
        if loop_id is not None:
            candidates = candidates.where(AgentLoop.loop_id == loop_id)
        for identity in (await session.scalars(candidates.order_by(AgentLoop.loop_id))).all():
            loop = await session.scalar(select(AgentLoop).where(AgentLoop.loop_id == identity)
                .with_for_update(skip_locked=True).execution_options(populate_existing=True))
            if loop is None:
                continue
            head = await session.get(LoopProgressHead, identity, with_for_update=True, populate_existing=True)
            first = await self._first(session, identity)
            if first is None:
                continue
            work = await session.get(LoopProgressWork, first.work_id,
                                     with_for_update=True, populate_existing=True)
            if work.state in {"claimed", "superseding"} and (
                    work.lease_expires_at is None or work.lease_expires_at <= now):
                await self._settle_unknown(session, work, now)
                if work.state == "superseding":
                    work.state, work.lease_expires_at = "superseded", None
                    continue
            if work.state not in {"pending", "claimed"} or (work.state == "claimed" and
                    work.lease_expires_at is not None and work.lease_expires_at > now):
                continue
            if work.attempts >= policy.max_attempts:
                work.state, work.error, work.lease_expires_at = "blocked", work.error or "progress_memory_attempts_exhausted", None
                continue
            try:
                await self._bind(session, loop, head, work)
            except ValueError as exc:
                work.state, work.error, work.lease_expires_at = "blocked", str(exc)[:2000], None
                continue
            work.state = "claimed"
            work.fence += 1
            work.attempts += 1
            work.lease_expires_at = now + timedelta(seconds=policy.lease_seconds)
            work.attempt_events = [*work.attempt_events, {"event": "claimed", "fence": work.fence,
                "attempt": work.attempts, "at": now.isoformat()}]
            return work.work_id, work.fence
        return None

    @staticmethod
    async def _settle_unknown(session, work, now):
        from backend.app.desktop.agent_loop.accounting_events import LoopAccountingEventRecorder

        entries = [dict(item) for item in work.usage]
        expired = []
        for item in entries:
            if item.get("accounted"):
                continue
            item.update(accounted=True, usage_reported=False, settlement="unknown")
            expired.append(item["fence"])
        work.usage = entries
        work.attempt_events = [*work.attempt_events, {"event": "lease_expired", "fence": work.fence,
            "at": now.isoformat()}]
        for fence in expired:
            await LoopAccountingEventRecorder().record(session, work.loop_id,
                source_kind="progress_memory", source_id=f"{work.work_id}:{fence}", transition="unknown")

    async def _bind(self, session, loop, head, work):
        if work.consolidation_payload is not None:
            if canonical_hash(work.consolidation_payload) != work.consolidation_hash:
                raise ValueError("progress_memory_consolidation_hash_mismatch")
            return
        if head is None:
            raise ValueError("progress_memory_missing_head")
        previous = await session.get(LoopTaskProgress, head.progress_id)
        manifest = TaskDeltaManifest.model_validate(work.source_manifest)
        receipts = dict((await session.execute(select(LoopProgressReceipt.source_key, LoopProgressReceipt.progress_id)
            .where(LoopProgressReceipt.loop_id == loop.loop_id,
                   LoopProgressReceipt.source_key.in_([s.source_key for s in manifest.sources])))).all())
        current = await TaskDeltaSourceReader().capture(session, loop, boundary=work.boundary_key)
        if not current.complete:
            raise ValueError(current.blocker)
        sources = {s.source_key: s for s in manifest.sources if s.source_key not in receipts}
        sources.update({s.source_key: s for s in current.sources if s.kind == "user_revision"})
        manifest = manifest.model_copy(update={"sources": tuple(sources.values())})
        mission = await session.scalar(select(LoopMissionRevision).where(
            LoopMissionRevision.loop_id == loop.loop_id, LoopMissionRevision.revision == loop.goal_revision))
        legacy = await session.scalar(select(LoopGoalRevision).where(
            LoopGoalRevision.loop_id == loop.loop_id, LoopGoalRevision.revision == loop.goal_revision)) if mission is None else None
        effective = EffectiveMissionProjector.from_rows(structured=mission, legacy=legacy).model_payload()
        if effective["revision"] < previous.document["mission_revision"]:
            raise ValueError("progress_memory_mission_regression")
        context_ids = tuple((await session.scalars(select(LoopContextMembership.context_id)
            .where(LoopContextMembership.loop_id == loop.loop_id))).all())
        reader = CommittedLineageReader()
        lineage = await reader.snapshot(session, await reader.published_roots(session, context_ids), workspace_id=loop.workspace_id)
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == loop.authority_revision))
        observation = await session.get(LoopObservation, work.observation_id) if work.observation_id else None
        budget = dict((observation.envelope.get("budget") or {}) if observation else {})
        budget["limits"] = grant.budgets if grant else {}
        budget.setdefault("progress_model_name", (loop.equipment or {}).get("model_name"))
        inputs = ConsolidationInputs(work_id=work.work_id,
            origin_observation_id=work.observation_id,
            origin_observation_hash=observation.envelope_hash if observation else None,
            origin_round_id=observation.round_id if observation else None,
            round_ids=tuple(work.round_ids), mission=effective, budget=budget,
            previous_progress_id=previous.progress_id, previous_progress_hash=previous.content_hash,
            previous_progress=TaskProgressDocument.model_validate(previous.document),
            task_delta=manifest, manifest_hash=canonical_hash(manifest),
            lineage=lineage, topology_hash=lineage["topology_hash"], absorbed_sources=receipts)
        work.consolidation_payload = inputs.model_dump(mode="json")
        work.consolidation_hash = canonical_hash(inputs)
