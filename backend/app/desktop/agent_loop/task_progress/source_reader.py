"""本文件对外提供 TaskDeltaSourceReader，从权威领域结果完整冻结未吸收增量。

输入为一致事务、Loop 与来源边界；输出为精确版本的 TaskDeltaManifest。
具体工作流为补录遗留终态来源与直接用户修订，按稳定 key 分页读取独立领域结果并排除吸收 receipts；
容量不足明确 blocker，不使用近期 Run 切片、Fact 表或展示 journal。
示例：await reader.capture(session, loop, boundary="snapshot-id")。
"""

from __future__ import annotations

from sqlalchemy import String, cast, exists, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.desktop.agent_loop.mission_models import LoopMissionRevision
from backend.app.desktop.agent_loop.models import AgentLoop, LoopUserIntent
from backend.app.desktop.agent_loop.task_progress.contracts import (
    TaskDeltaManifest,
    TaskSource,
)
from backend.app.desktop.agent_loop.task_progress.models import LoopProgressReceipt
from backend.app.desktop.context_evolution.models import ContextRevision
from backend.app.desktop.domain_evidence.models import DesktopDomainResult
from backend.app.desktop.domain_evidence.repository import DomainResultRepository
from backend.app.desktop.domain_evidence.run_outcomes import RunOutcomeRecorder
from backend.app.desktop.models import DesktopRun


class TaskDeltaSourceReader:
    def __init__(self, max_sources: int = 4096) -> None:
        self._max_sources = max_sources
        self._results = DomainResultRepository()

    async def capture(
        self, session: AsyncSession, loop: AgentLoop, *, boundary: str
    ) -> TaskDeltaManifest:
        await self._legacy_runs(session, loop.loop_id)
        await self._user_revisions(session, loop.loop_id)
        absorbed = exists(
            select(LoopProgressReceipt.source_key).where(
                LoopProgressReceipt.loop_id == loop.loop_id,
                LoopProgressReceipt.source_key == DesktopDomainResult.result_key,
            )
        )
        base = (
            select(DesktopDomainResult, DesktopRun.round_id)
            .outerjoin(DesktopRun, DesktopRun.run_id == DesktopDomainResult.run_id)
            .where(
                or_(
                    DesktopDomainResult.loop_id == loop.loop_id,
                    DesktopRun.loop_id == loop.loop_id,
                ),
                ~absorbed,
            )
        )
        sources = []
        cursor = ""
        while True:
            rows = (
                await session.execute(
                    base.where(DesktopDomainResult.result_key > cursor)
                    .order_by(DesktopDomainResult.result_key)
                    .limit(256)
                )
            ).all()
            if not rows:
                break
            for row, execution_round_id in rows:
                sources.append(
                    TaskSource(
                        source_key=row.result_key,
                        kind=row.kind,
                        source_id=row.source_id,
                        version=row.version,
                        context_id=row.context_id,
                        run_id=row.run_id,
                        execution_round_id=execution_round_id,
                        payload=row.payload,
                    )
                )
                if len(sources) > self._max_sources:
                    return TaskDeltaManifest(
                        boundary=boundary,
                        complete=False,
                        sources=tuple(sources),
                        blocker="task_source_budget_exceeded: 来源枚举未完成",
                    )
            cursor = rows[-1][0].result_key
        return TaskDeltaManifest(boundary=boundary, sources=tuple(sources))

    async def _legacy_runs(self, session: AsyncSession, loop_id: str) -> None:
        recorded = exists(
            select(DesktopDomainResult.result_key).where(
                DesktopDomainResult.kind == "run_outcome",
                DesktopDomainResult.source_id == DesktopRun.run_id,
            )
        )
        cursor = ""
        recorder = RunOutcomeRecorder()
        while True:
            runs = tuple(
                (
                    await session.scalars(
                        select(DesktopRun)
                        .where(
                            DesktopRun.loop_id == loop_id,
                            DesktopRun.kind == "main",
                            DesktopRun.status.not_in(["pending", "running"]),
                            DesktopRun.settled_at.is_not(None),
                            DesktopRun.run_id > cursor,
                            ~recorded,
                        )
                        .order_by(DesktopRun.run_id)
                        .limit(256)
                    )
                ).all()
            )
            if not runs:
                return
            for run in runs:
                revision = await session.scalar(
                    select(ContextRevision)
                    .where(
                        ContextRevision.origin_kind == "run_settled",
                        ContextRevision.origin_id == run.run_id,
                    )
                    .order_by(ContextRevision.generation.desc())
                    .limit(1)
                )
                await recorder.record(
                    session, run, tuple(revision.authored_messages) if revision else ()
                )
            cursor = runs[-1].run_id

    async def _user_revisions(self, session: AsyncSession, loop_id: str) -> None:
        recorded = exists(
            select(DesktopDomainResult.result_key).where(
                DesktopDomainResult.kind == "user_revision",
                DesktopDomainResult.source_id == LoopUserIntent.intent_id,
            )
        )
        intents = tuple(
            (
                await session.scalars(
                    select(LoopUserIntent).where(
                        LoopUserIntent.loop_id == loop_id, ~recorded
                    )
                )
            ).all()
        )
        for intent in intents:
            await self._results.record(
                session,
                kind="user_revision",
                source_id=intent.intent_id,
                loop_id=loop_id,
                context_id=intent.target_context_id,
                payload={
                    "scope": intent.scope,
                    "instruction": intent.content,
                    "mission_revision": intent.goal_revision,
                },
            )
        mission_recorded = exists(
            select(DesktopDomainResult.result_key).where(
                DesktopDomainResult.kind == "user_revision",
                DesktopDomainResult.source_id
                == literal(f"mission:{loop_id}:")
                + cast(LoopMissionRevision.revision, String),
            )
        )
        revisions = tuple(
            (
                await session.scalars(
                    select(LoopMissionRevision).where(
                        LoopMissionRevision.loop_id == loop_id, ~mission_recorded
                    )
                )
            ).all()
        )
        for revision in revisions:
            await self._results.record(
                session,
                kind="user_revision",
                source_id=f"mission:{loop_id}:{revision.revision}",
                loop_id=loop_id,
                context_id=None,
                payload={
                    "mission_revision": revision.revision,
                    "mission": {
                        "outcome": revision.outcome,
                        "boundaries": revision.boundaries,
                        "completion_checks": revision.completion_checks,
                    },
                },
            )
