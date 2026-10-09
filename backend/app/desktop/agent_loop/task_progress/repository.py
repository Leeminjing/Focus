"""本文件对外提供 TaskProgressRepository 的初始、冻结、readiness、授权重试和原子发布端口。

输入为调用方短事务、不可变合同、fence 和有界安全候选诊断；输出为按尝试诊断、唯一版本或明确 stale/readiness 错误。
具体工作流为 Loop→head→work 锁序，诊断复检有效领取及冻结身份，重试复检预算授权并保留历史反馈和消费，前序 CAS、输入 hash 和租约共同验证，再原子插入版本与 receipts。
无模型调用或 Context 发布权限。示例：await repository.publish(session, observation_id, fence, document, contribution)。
Progress head、来源 receipts 与 task_progress.published journal 在同一发布事务提交，读面可独立于用户发送和执行事实更新。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.models import AgentLoop, LoopObservation, LoopDelegationGrant
from backend.app.desktop.agent_loop.task_progress.contracts import (
    RoundContribution,
    RoundDecisionInputs,
    TaskProgressDocument,
    canonical_hash,
)
from backend.app.desktop.agent_loop.task_progress.models import (
    LoopDecisionInputs,
    LoopProgressHead,
    LoopProgressReceipt,
    LoopProgressWork,
    LoopTaskProgress,
)
from backend.app.desktop.agent_loop.task_progress.candidate_interpretation import legacy_candidate_failure


class ProgressNotReady(RuntimeError):
    def __init__(self, observation_id, state, error=None):
        self.observation_id = observation_id
        self.state = state
        self.failure_kind = error.split(":", 1)[0] if error else None
        super().__init__(f"progress_memory_{state}: {observation_id}; {error or '等待上一轮任务记忆'}")


class ProgressPublicationRejected(RuntimeError):
    pass


class TaskProgressRepository:
    async def current(
        self, session: AsyncSession, loop_id: str
    ) -> LoopTaskProgress | None:
        return await session.scalar(
            select(LoopTaskProgress)
            .join(
                LoopProgressHead,
                LoopProgressHead.progress_id == LoopTaskProgress.progress_id,
            )
            .where(LoopProgressHead.loop_id == loop_id)
        )

    async def initialize(
        self,
        session: AsyncSession,
        loop_id: str,
        document: TaskProgressDocument,
        *,
        source_keys: tuple[str, ...] = (),
    ) -> LoopTaskProgress:
        existing = await self.current(session, loop_id)
        if existing is not None:
            return existing
        row = LoopTaskProgress(
            progress_id=uuid.uuid4().hex,
            loop_id=loop_id,
            generation=0,
            document=document.model_dump(mode="json"),
            content_hash=canonical_hash(document),
            contribution={},
        )
        session.add(row)
        await session.flush()
        session.add(LoopProgressHead(loop_id=loop_id, progress_id=row.progress_id))
        session.add_all(
            LoopProgressReceipt(
                loop_id=loop_id, source_key=key, progress_id=row.progress_id
            )
            for key in source_keys
        )
        await session.flush()
        return row

    async def require_ready(self, session: AsyncSession, loop_id: str) -> None:
        unfinished = await session.scalar(
            select(LoopProgressWork)
            .where(
                LoopProgressWork.loop_id == loop_id,
                LoopProgressWork.state != "published",
            )
            .order_by(LoopProgressWork.created_at)
            .limit(1)
        )
        if unfinished is not None:
            raise ProgressNotReady(unfinished.observation_id, unfinished.state, unfinished.error)

    async def retry(self, session, observation_id, *, loop_id=None):
        identity = await session.get(LoopProgressWork, observation_id)
        if identity is None or (loop_id is not None and identity.loop_id != loop_id):
            raise ValueError("任务记忆工作不存在或不属于本 Loop")
        loop = await session.get(AgentLoop, identity.loop_id, with_for_update=True)
        work = await session.get(LoopProgressWork, observation_id, with_for_update=True, populate_existing=True)
        if work.state != "blocked":
            raise ValueError("只有明确 blocked 的任务记忆工作可以恢复")
        grant = await session.scalar(select(LoopDelegationGrant).where(
            LoopDelegationGrant.loop_id == loop.loop_id, LoopDelegationGrant.revision == loop.authority_revision,
            LoopDelegationGrant.status == "active"))
        if loop.status not in {"running", "paused", "waiting_user"} or grant is None or (
            grant.expires_at is not None and grant.expires_at <= datetime.now(UTC)):
            raise ValueError("显式重试需要有效的预算授权")
        work.retry_budget_authorization = {"grant_id": grant.grant_id, "grant_revision": grant.revision,
            "limits": grant.budgets, "approved_at": datetime.now(UTC).isoformat()}
        work.attempt_events = [*work.attempt_events, {"event": "explicit_retry",
            "budget_authorization": work.retry_budget_authorization,
            "legacy_candidate_failure": legacy_candidate_failure(work.error)}]
        work.state = "pending"
        work.attempts = 0
        work.error = None
        work.lease_expires_at = None

    async def freeze(
        self, session: AsyncSession, loop_id: str, inputs: RoundDecisionInputs
    ) -> None:
        session.add(
            LoopDecisionInputs(
                observation_id=inputs.observation_id,
                loop_id=loop_id,
                round_id=inputs.round_id,
                payload=inputs.model_dump(mode="json"),
                content_hash=canonical_hash(inputs),
            )
        )
        await session.flush()
        session.add(
            LoopProgressWork(observation_id=inputs.observation_id, loop_id=loop_id)
        )

    async def inputs(
        self, session: AsyncSession, observation_id: str
    ) -> RoundDecisionInputs:
        row = await session.get(LoopDecisionInputs, observation_id)
        if row is None or canonical_hash(row.payload) != row.content_hash:
            raise ProgressPublicationRejected("冻结输入缺失或内容 hash 不一致")
        return RoundDecisionInputs.model_validate(row.payload)

    async def record_validation(self, session, inputs, fence, diagnostic):
        identity = await session.get(LoopProgressWork, inputs.observation_id)
        if identity is None:
            raise ProgressPublicationRejected("进度工作不存在")
        await session.get(AgentLoop, identity.loop_id, with_for_update=True)
        work = await session.get(LoopProgressWork, inputs.observation_id, with_for_update=True, populate_existing=True)
        if (work.state != "claimed" or work.fence != fence or work.lease_expires_at is None
                or work.lease_expires_at <= datetime.now(UTC)):
            raise ProgressPublicationRejected("进度诊断 worker fence/lease 已失效")
        frozen = await self.inputs(session, inputs.observation_id)
        if (frozen != inputs or diagnostic["inputs_hash"] != canonical_hash(frozen)
                or diagnostic["manifest_hash"] != frozen.manifest_hash or diagnostic["fence"] != fence):
            raise ProgressPublicationRejected("进度诊断冻结身份不一致")
        work.attempt_events = [*work.attempt_events, diagnostic]

    async def publish(
        self,
        session: AsyncSession,
        observation_id: str,
        fence: int,
        document: TaskProgressDocument,
        contribution: RoundContribution,
    ) -> LoopTaskProgress:
        work_identity = await session.get(LoopProgressWork, observation_id)
        if work_identity is None:
            raise ProgressPublicationRejected("进度工作不存在")
        await session.get(AgentLoop, work_identity.loop_id, with_for_update=True)
        head = await session.get(
            LoopProgressHead, work_identity.loop_id, with_for_update=True
        )
        work = await session.get(
            LoopProgressWork,
            observation_id,
            with_for_update=True,
            populate_existing=True,
        )
        existing = await session.scalar(
            select(LoopTaskProgress).where(
                LoopTaskProgress.observation_id == observation_id
            )
        )
        if existing is not None:
            return existing
        if (
            work.state != "claimed"
            or work.fence != fence
            or work.lease_expires_at is None
            or work.lease_expires_at <= datetime.now(UTC)
        ):
            raise ProgressPublicationRejected("进度 worker fence/lease 已失效")
        inputs = await self.inputs(session, observation_id)
        observation = await session.get(LoopObservation, observation_id)
        if observation is None or observation.envelope_hash != inputs.observation_hash:
            raise ProgressPublicationRejected("基础 Observation 被改写")
        if (
            head is None
            or head.progress_id != inputs.previous_progress_id
            or contribution.round_id != inputs.round_id
        ):
            raise ProgressPublicationRejected("进度前序 CAS 或 Round 归属失效")
        previous = await session.get(LoopTaskProgress, head.progress_id)
        if previous.content_hash != inputs.previous_progress_hash:
            raise ProgressPublicationRejected("前序内容 hash 不一致")
        from backend.app.desktop.agent_loop.task_progress.consolidation import (
            TaskProgressConsolidator,
        )
        from backend.app.desktop.agent_loop.task_progress.contracts import (
            ProgressCandidate,
        )

        candidate = ProgressCandidate(
            changes=contribution.changes,
            source_assessments=contribution.source_assessments,
        )
        expected, expected_contribution = TaskProgressConsolidator().apply(
            inputs, candidate, observation.envelope.get("mission") or {}
        )
        if document != expected or contribution != expected_contribution:
            raise ProgressPublicationRejected("候选正文或贡献不符合冻结来源验证")
        row = LoopTaskProgress(
            progress_id=uuid.uuid4().hex,
            loop_id=work.loop_id,
            generation=previous.generation + 1,
            observation_id=observation_id,
            previous_progress_id=previous.progress_id,
            document=document.model_dump(mode="json"),
            content_hash=canonical_hash(document),
            contribution=contribution.model_dump(mode="json"),
        )
        session.add(row)
        await session.flush()
        session.add_all(
            LoopProgressReceipt(
                loop_id=work.loop_id,
                source_key=source.source_key,
                progress_id=row.progress_id,
            )
            for source in inputs.task_delta.sources
        )
        head.progress_id = row.progress_id
        work.state = "published"
        work.lease_expires_at = None
        work.error = None
        work.attempt_events = [
            *work.attempt_events,
            {
                "event": "published",
                "fence": fence,
                "progress_id": row.progress_id,
                "at": datetime.now(UTC).isoformat(),
            },
        ]
        from backend.app.desktop.agent_loop.event_contract import CanonicalEventDraft
        from backend.app.desktop.agent_loop.event_journal import LoopEventJournal
        await LoopEventJournal().append(session, work.loop_id, CanonicalEventDraft(
            kind="task_progress.published", entity_type="task_progress", entity_id=work.loop_id,
            entity_revision=row.generation + 1, idempotency_key=f"progress:{row.progress_id}:published",
            payload={"progress_id": row.progress_id, "generation": row.generation,
                     "content_hash": row.content_hash, "document": row.document}))
        return row
