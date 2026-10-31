"""本文件对外提供 EvidenceJob、EvidenceJobRepository 与精确来源匹配。

输入为冻结 Observation、Context 范围及 Worker attempt；输出为持久任务、版本化证据和提交后的唤醒事实。
具体工作流为在既有 Worker 队列记录依赖、预算 owner、逐次 fence 与不可变结果，完成事务同时发布 Outbox 和 Round 状态。
示例：await EvidenceJobRepository().finish(session, worker, "ready", result)。
Worker 继续负责执行、预算准入和结算，证据任务本身不授予决策权。
"""

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, mapped_column

from focus.persistence.base import Base
from backend.app.desktop.patrol.agent_loop.task_progress.contracts import canonical_hash


def source_revisions(observation, context_ids=()):
    requested = set(context_ids)
    frontier = {item["context_id"]: item for item in observation.portfolio_frontier if item.get("revision_id")}
    if requested - frontier.keys():
        raise ValueError("evidence_source_unknown: Context 不在冻结 Observation 中")
    return {context_id: dict(item.get("revision") or {"context_id": context_id, "revision_id": item["revision_id"]})
            for context_id, item in sorted(frontier.items()) if not requested or context_id in requested}


def evidence_matches(observation, result):
    required = result.get("required_source_revisions")
    return (result.get("observation_id") == (observation.observation_id or observation.decision_inputs_ref)
            and result.get("observation_generation") == observation.generation
            and result.get("round_id") == observation.round_id
            and isinstance(required, dict) and bool(required)
            and all(source_revisions(observation).get(key) == value for key, value in required.items())
            and result.get("result_hash") == canonical_hash(result.get("result") or {}))


class EvidenceJob(Base):
    __tablename__ = "loop_evidence_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('pending','running','ready','failed','cancelled','superseded')", name="ck_evidence_job_status"),
        Index("ix_evidence_job_observation_status", "observation_id", "status"),
    )

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    loop_id: Mapped[str] = mapped_column(String(32), ForeignKey("agent_loops.loop_id", ondelete="CASCADE"), nullable=False)
    round_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_rounds.round_id", ondelete="CASCADE"), nullable=False)
    observation_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_observations.observation_id", ondelete="CASCADE"), nullable=False)
    observation_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    required_source_revisions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    budget_reservation: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    worker_request_id: Mapped[str] = mapped_column(String(32), ForeignKey("loop_worker_requests.worker_request_id", ondelete="CASCADE"), nullable=False, unique=True)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvidenceJobRepository:
    async def adopt_legacy(self, session, observation):
        """Recover exact-bound pre-upgrade Curators in a writable short transaction."""
        from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopRound, LoopWorkerRequest
        from backend.app.desktop.patrol.agent_loop.sessions.patrol_session_models import LoopCuratorAssignment

        await session.get(AgentLoop, observation.loop_id, with_for_update=True)
        current = await session.get(LoopRound, observation.round_id, with_for_update=True)
        if (current.observation_id != (observation.observation_id or observation.decision_inputs_ref)
                or current.decision_id is not None or current.status in {"settled", "error", "superseded"}):
            return
        rows = await session.execute(select(LoopWorkerRequest, LoopCuratorAssignment)
            .join(LoopCuratorAssignment, LoopCuratorAssignment.worker_request_id == LoopWorkerRequest.worker_request_id)
            .outerjoin(EvidenceJob, EvidenceJob.worker_request_id == LoopWorkerRequest.worker_request_id)
            .where(LoopWorkerRequest.observation_id == current.observation_id,
                LoopWorkerRequest.observation_generation == observation.generation,
                LoopWorkerRequest.kind == "lane_curator",
                or_(EvidenceJob.job_id.is_(None),
                    (LoopWorkerRequest.status == "success") & (EvidenceJob.status != "ready"))))
        for worker, assignment in rows:
            context_id = assignment.scope["context_id"]
            try:
                required = source_revisions(observation, (context_id,))
            except ValueError:
                continue
            if (required[context_id]["revision_id"] != assignment.scope["revision_id"]
                    or not worker.retry_identity and worker.status == "success"):
                continue
            job = await self.ensure(session, observation, "curator", (context_id,), worker.worker_request_id)
            if job.worker_request_id != worker.worker_request_id:
                continue
            if worker.status in {"running", "success"} and not worker.scope.get("evidence_job_id"):
                await self.claim(session, worker)
            if worker.status == "success":
                await self.finish(session, worker, "ready", worker.result)
            elif worker.status in {"error", "cancelled", "superseded"}:
                job.status = "failed" if worker.status == "error" else worker.status

    async def ensure(self, session, observation, kind, context_ids, worker_id):
        required = source_revisions(observation, context_ids)
        identity = observation.observation_id or observation.decision_inputs_ref
        job_id = canonical_hash([identity, observation.generation, kind, required])
        await session.execute(insert(EvidenceJob).values(
            job_id=job_id, loop_id=observation.loop_id, round_id=observation.round_id,
            observation_id=identity, observation_generation=observation.generation,
            required_source_revisions=required, evidence_kind=kind, worker_request_id=worker_id,
            budget_reservation={"owner_kind": "worker", "owner_id": worker_id,
                                "grant_revision": observation.authority_revision},
        ).on_conflict_do_nothing(index_elements=[EvidenceJob.job_id]))
        return await session.get(EvidenceJob, job_id, populate_existing=True)

    async def by_worker(self, session, worker_id):
        return await session.scalar(select(EvidenceJob).where(EvidenceJob.worker_request_id == worker_id).with_for_update())

    async def claim(self, session, worker):
        job = await self.by_worker(session, worker.worker_request_id)
        if job is None:
            return
        if job.status == "ready":
            raise ValueError("evidence_result_immutable")
        job.fencing_token += 1
        job.status = "running"
        job.budget_reservation = {**job.budget_reservation, "retry_identity": worker.retry_identity}
        worker.scope = {**worker.scope, "evidence_job_id": job.job_id, "evidence_fencing_token": job.fencing_token}

    async def finish(self, session, worker, status, result):
        from backend.app.desktop.patrol.agent_loop.models import AgentLoop

        await session.get(AgentLoop, worker.loop_id, with_for_update=True)
        job = await self.by_worker(session, worker.worker_request_id)
        if job is None:
            return
        if status == "ready":
            from backend.app.desktop.patrol.agent_loop.workers.worker_attempts import WorkerAttemptAuthority
            from backend.app.desktop.patrol.agent_loop.models import LoopObservation
            from backend.app.desktop.patrol.agent_loop.schemas import LoopObservationEnvelope

            await WorkerAttemptAuthority().require_current(session, worker, statuses=("running", "success"))
            base = await session.get(LoopObservation, worker.observation_id)
            frozen = LoopObservationEnvelope.model_validate(base.envelope)
            if (job.observation_id != worker.observation_id or job.observation_generation != worker.observation_generation
                    or job.required_source_revisions != source_revisions(frozen, tuple(job.required_source_revisions))):
                raise ValueError("evidence_source_identity_mismatch")
        if (job.fencing_token != worker.scope.get("evidence_fencing_token")
                or job.budget_reservation.get("retry_identity") != worker.retry_identity):
            raise ValueError("evidence_fencing_rejected")
        if job.status == "ready":
            if job.result_hash != canonical_hash(result):
                raise ValueError("evidence_result_immutable")
            return
        job.status = status
        if status == "ready":
            job.result, job.result_hash = result, canonical_hash(result)
            job.completed_at = datetime.now(UTC)
            if job.evidence_kind == "expansion_assessment" and job.required_source_revisions == source_revisions(frozen):
                from backend.app.desktop.patrol.agent_loop.decisions.decision_context import DecisionSupplementRepository

                await DecisionSupplementRepository().put(session, job.observation_id, "expansion_assessment", result["assessment"])
            await session.flush()
            await self._notify(session, job)

    async def _notify(self, session, job):
        from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopEventOutbox, LoopRound

        loop = await session.get(AgentLoop, job.loop_id, with_for_update=True)
        current = await session.get(LoopRound, job.round_id, with_for_update=True)
        if (job.evidence_kind in {"curator", "expansion_assessment"} and current.decision_id is None
                and current.observation_id == job.observation_id and current.status not in {"settled", "superseded", "error"}):
            from backend.app.desktop.patrol.agent_loop.journal.lifecycle_events import LoopLifecycleEventRecorder
            from backend.app.desktop.patrol.agent_loop.journal.round_events import RoundStateEventRecorder

            current.status, loop.health = "curated", "deciding"
            await RoundStateEventRecorder().record(session, current)
            await LoopLifecycleEventRecorder().record(session, loop)
        sequence = int(await session.scalar(select(func.coalesce(func.max(LoopEventOutbox.sequence), 0))
                                           .where(LoopEventOutbox.loop_id == job.loop_id)) or 0) + 1
        session.add(LoopEventOutbox(event_id=job.job_id[:32], loop_id=job.loop_id, sequence=sequence,
            event_type="EvidenceJobReady", payload={"job_id": job.job_id, "round_id": job.round_id,
                "observation_id": job.observation_id, "observation_generation": job.observation_generation,
                "result_hash": job.result_hash}, idempotency_key=f"evidence:{job.job_id}:ready"))

    async def results(self, session, observation):
        jobs = await session.scalars(select(EvidenceJob).where(
            EvidenceJob.observation_id == (observation.observation_id or observation.decision_inputs_ref),
            EvidenceJob.observation_generation == observation.generation,
            EvidenceJob.evidence_kind == "curator", EvidenceJob.status == "ready").order_by(EvidenceJob.job_id))
        results = []
        for job in jobs:
            result = {"request_id": job.worker_request_id, "round_id": job.round_id, "kind": "lane_curator",
                "status": "proposed", "result": job.result, "result_hash": job.result_hash,
                "observation_id": job.observation_id, "observation_generation": job.observation_generation,
                "required_source_revisions": job.required_source_revisions, "evidence_job_id": job.job_id}
            if evidence_matches(observation, result):
                results.append(result)
        return tuple(results)
