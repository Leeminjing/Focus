r"""本文件对外提供 CompressionResolutionCoordinator 的 leased 恢复执行与 Run 结算收口。

输入为 committed/resuming resolution、已接受 candidate、精确未决 compression interrupt 和稳定
MainRunSettled event；输出为唯一 resume Run 与 applied/superseded/failed resolution。具体工作流为
skip-locked 领取并 fencing，规范 payload 只从 candidate 构造，经 DesktopService.resume_run 和统一
execute_prepared_run 启动；结算后记录 checkpoint/revision 并解开 Loop。示例：
`await coordinator.drain(); await coordinator.settle_run(session, event, run, loop)`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.compression_authority.models import LoopCompressionCandidate, LoopCompressionResolution
from backend.app.desktop.agent_loop.models import AgentLoop, LoopAction, LoopPendingDecision, LoopRound
from backend.app.desktop.context_evolution import ContextRevisionRepository
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.run_orchestration import RunExecutionResources, RunLifecycleFinalizer, execute_prepared_run


logger = logging.getLogger(__name__)


class CompressionResolutionCoordinator:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], desktop_service, *, lease_seconds: int = 90) -> None:
        self._sessions = sessions
        self._desktop = desktop_service
        self._lease_seconds = lease_seconds
        self._owner = f"compression-resolution:{uuid.uuid4().hex}"
        self._revisions = ContextRevisionRepository()

    async def drain(self) -> int:
        claim = await self._claim()
        if claim is None:
            return 0
        resolution_id, lease_token = claim
        try:
            await self._launch(resolution_id, lease_token)
        except Exception as exc:
            logger.exception("自主 compression resolution 启动失败")
            await self._fail(resolution_id, lease_token, exc)
        return 1

    async def settle_run(self, session: AsyncSession, event, run, loop) -> str | None:
        resolution = await session.scalar(
            select(LoopCompressionResolution)
            .where(LoopCompressionResolution.resume_run_id == run.run_id)
            .with_for_update()
        )
        if resolution is None or resolution.status not in {"resuming", "applied"}:
            return None
        if resolution.status == "applied":
            return "applied"
        pending = await session.get(LoopPendingDecision, resolution.pending_decision_id, with_for_update=True)
        action = await session.get(LoopAction, resolution.action_id, with_for_update=True)
        round_row = await session.get(LoopRound, resolution.round_id, with_for_update=True)
        revision = (event.payload or {}).get("context_revision") or {}
        if run.status != "success" or not revision.get("revision_id"):
            resolution.status = "failed"
            resolution.error_evidence = {"run_id": run.run_id, "status": run.status, "error": run.error}
            if pending is not None:
                pending.status = "failed"
            if action is not None:
                action.status = "failed"
                action.result = {"resolution_id": resolution.resolution_id, "error": run.error or run.status}
            if round_row is not None:
                round_row.status = "error"
            loop.status = "waiting_user"
            loop.health = "degraded"
            loop.waiting_reason = "Patrol 自主压缩恢复失败，需要用户决定"
            return "failed"
        await self._mark_applied(
            session,
            resolution,
            run,
            loop,
            revision.get("checkpoint_id") or run.final_checkpoint_id,
            revision.get("revision_id"),
            pending=pending,
            action=action,
            round_row=round_row,
        )
        return "applied"

    async def reconcile(self) -> int:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            rows = list(
                (
                    await session.scalars(
                        select(LoopCompressionResolution)
                        .where(
                            LoopCompressionResolution.status.in_(["committed", "resuming"]),
                            (LoopCompressionResolution.lease_expires_at.is_(None) | (LoopCompressionResolution.lease_expires_at <= now)),
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            recovered = 0
            for row in rows:
                if row.status == "resuming" and row.resume_run_id:
                    run = await session.get(DesktopRun, row.resume_run_id)
                    if run is not None and run.status == "success" and run.settled_at is not None:
                        current = await self._revisions.current(session, run.task_id)
                        if (
                            current is not None
                            and current.ref.checkpoint_id == run.final_checkpoint_id
                            and current.origin_id == run.run_id
                        ):
                            loop = await session.get(AgentLoop, row.loop_id, with_for_update=True)
                            if loop is not None:
                                await self._mark_applied(
                                    session,
                                    row,
                                    run,
                                    loop,
                                    current.ref.checkpoint_id,
                                    current.ref.revision_id,
                                )
                                recovered += 1
                                continue
                    if run is not None and run.status in {"running", "success"}:
                        continue
                    if run is not None and run.status == "pending":
                        row.status = "committed"
                        recovered += 1
                        continue
                    if run is not None and run.status in {"error", "interrupted"}:
                        row.status = "failed"
                        row.error_evidence = {"reason": "resume_run_terminal", "run_id": run.run_id, "status": run.status}
                        recovered += 1
                row.lease_owner = None
                row.lease_token = None
                row.lease_expires_at = None
            return recovered

    async def _mark_applied(
        self,
        session: AsyncSession,
        resolution: LoopCompressionResolution,
        run: DesktopRun,
        loop: AgentLoop,
        checkpoint_id: str | None,
        revision_id: str | None,
        *,
        pending: LoopPendingDecision | None = None,
        action: LoopAction | None = None,
        round_row: LoopRound | None = None,
    ) -> None:
        candidate = await session.get(LoopCompressionCandidate, resolution.candidate_id)
        pending = pending or await session.get(LoopPendingDecision, resolution.pending_decision_id, with_for_update=True)
        action = action or await session.get(LoopAction, resolution.action_id, with_for_update=True)
        round_row = round_row or await session.get(LoopRound, resolution.round_id, with_for_update=True)
        resolution.status = "applied"
        resolution.result_checkpoint_id = checkpoint_id
        resolution.result_context_revision_id = revision_id
        resolution.actual_before_tokens = candidate.before_tokens if candidate else None
        resolution.actual_after_tokens = candidate.after_tokens if candidate else None
        resolution.lease_owner = None
        resolution.lease_token = None
        resolution.lease_expires_at = None
        if pending is not None:
            pending.status = "resolved"
        if action is not None:
            action.status = "applied"
            action.result = {
                "resolution_id": resolution.resolution_id,
                "run_id": run.run_id,
                "checkpoint_id": checkpoint_id,
                "context_revision_id": revision_id,
            }
        if round_row is not None:
            round_row.status = "running"
        loop.health = "waiting_runs"

    async def _claim(self) -> tuple[str, str] | None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            resolution = await session.scalar(
                select(LoopCompressionResolution)
                .where(
                    LoopCompressionResolution.status == "committed",
                    (LoopCompressionResolution.lease_expires_at.is_(None) | (LoopCompressionResolution.lease_expires_at <= now)),
                )
                .order_by(LoopCompressionResolution.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if resolution is None:
                return None
            token = uuid.uuid4().hex
            resolution.lease_owner = self._owner
            resolution.lease_token = token
            resolution.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            return resolution.resolution_id, token

    async def _launch(self, resolution_id: str, lease_token: str) -> None:
        async with self._sessions() as session:
            resolution = await session.get(LoopCompressionResolution, resolution_id)
            candidate = await session.get(LoopCompressionCandidate, resolution.candidate_id) if resolution else None
            loop = await session.get(AgentLoop, resolution.loop_id) if resolution else None
            task = await session.get(DesktopThread, candidate.context_id) if candidate else None
            if resolution is None or candidate is None or loop is None or task is None:
                raise LookupError("compression resolution 执行身份不存在")
            if resolution.lease_token != lease_token or resolution.status != "committed":
                return
            run_id = resolution.resume_run_id or resolution.resolution_id
            payload = {"type": "compression", "decision": "apply", "ranges": candidate.normalized_ranges}
            identity = {
                "run_id": run_id,
                "origin": "delegated_patrol_compression",
                "loop_id": loop.loop_id,
                "round_id": resolution.round_id,
                "action_id": resolution.action_id,
                "resolution_id": resolution.resolution_id,
                "idempotency_key": resolution.idempotency_key,
            }
        prepared = await self._desktop.resume_run(task.thread_id, payload, identity)
        superseded = False
        async with self._sessions.begin() as session:
            current = await session.get(LoopCompressionResolution, resolution_id, with_for_update=True)
            if current is not None and current.status == "superseded":
                superseded = True
            elif current is None or current.status != "committed" or current.lease_token != lease_token:
                raise LookupError("compression resolution lease 已失效")
            else:
                current.status = "resuming"
                current.resume_run_id = run_id
        if superseded:
            await RunLifecycleFinalizer(self._sessions, self._desktop.checkpointer).abort_prepared(
                run_id,
                "compression resolution 已被用户操作取代",
            )
            return
        try:
            record = await execute_prepared_run(
                prepared.body,
                prepared.thread_id,
                RunExecutionResources(
                    bridge=self._desktop.bridge,
                    run_manager=self._desktop.run_manager,
                    checkpointer=self._desktop.checkpointer,
                    store=self._desktop.store,
                    app_config=self._desktop.app_config,
                ),
                prepared.agent_factory,
            )
            self._desktop.attach_run_sync(record)
        except Exception as exc:
            self._desktop.run_manager.cancel(run_id, action="interrupt")
            await RunLifecycleFinalizer(self._sessions, self._desktop.checkpointer).abort_prepared(run_id, str(exc))
            raise

    async def _fail(self, resolution_id: str, lease_token: str, exc: Exception) -> None:
        async with self._sessions.begin() as session:
            resolution = await session.get(LoopCompressionResolution, resolution_id, with_for_update=True)
            if (
                resolution is None
                or resolution.lease_token != lease_token
                or resolution.status == "superseded"
            ):
                return
            resolution.status = "failed"
            resolution.error_evidence = {"type": type(exc).__name__, "message": str(exc)[:2000]}
            pending = await session.get(LoopPendingDecision, resolution.pending_decision_id, with_for_update=True)
            action = await session.get(LoopAction, resolution.action_id, with_for_update=True)
            loop = await session.get(AgentLoop, resolution.loop_id, with_for_update=True)
            if pending is not None:
                pending.status = "failed"
            if action is not None:
                action.status = "failed"
            if loop is not None and loop.status == "running":
                loop.status = "waiting_user"
                loop.health = "degraded"
                loop.waiting_reason = "Patrol 自主压缩无法恢复，请由用户处理"
