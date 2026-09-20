r"""本文件对外提供 LoopWaveDispatcher、LoopRunWorkspaceBinder、DesktopDirectiveLaunchPort 与 DirectiveLaunchPort。

输入为 committed LoopDirective、Context revision、workspace slot/lease 和运行预算；输出为一波零到多条
绑定 round/action 的 DesktopRun。具体工作流为 Dispatcher 先以行锁认领 directive，WorkspaceRunPlanner
再把 Reader、权威 Writer 与授权的 Git 隔离 Writer 分配到不冲突的 slot；Binder 为所有 Loop Run 建立
lease/anchor，容量不足时持久记录 queued_reason，LaunchPort 在持有 Loop、directive、Run 权力锁时启动统一执行脊柱，
并以透明 activity bridge 将当前 Run 的 Tool/Artifact 生命周期提交 journal 后写入 launched 状态。
受治理工具根绑定真实 slot，来源元数据不进入模型消息。
示例：`run_ids = await dispatcher.dispatch(loop_id, round_id)`。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Protocol
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopDirective
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.directive_causality import DirectiveCausalityRecorder
from backend.app.desktop.agent_loop.provenance import DelegatedDirectiveFactory
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.run_orchestration import RunExecutionResources, RunLifecycleFinalizer, execute_prepared_run
from backend.app.desktop.workspace_coordination.leases import WorkspaceLeaseManager
from backend.app.desktop.workspace_coordination.models import RunExecutionAnchor, WorkspaceSlot
from backend.app.desktop.agent_loop.workspace_planning import WorkspaceRunPlanner
from backend.app.desktop.agent_loop.run_activity_bridge import LoopRunActivityBridge
from backend.app.desktop.workspace_coordination.schemas import WorkspaceIntentDeriver, WorkspaceLeaseRequest


class DirectiveLaunchPort(Protocol):
    async def __call__(self, directive: LoopDirective, message, slot_id: str) -> str: ...


class LoopWaveDispatcher:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], launcher: DirectiveLaunchPort) -> None:
        self._sessions = sessions
        self._launcher = launcher
        self._workspaces = WorkspaceRunPlanner(sessions)
        self._lifecycle = DirectiveLifecycleRepository()
        self._causality = DirectiveCausalityRecorder()

    async def dispatch(self, loop_id: str, round_id: str, concurrency: int) -> tuple[str, ...]:
        directives = await self._claim(loop_id, round_id, concurrency)
        if not directives:
            return ()
        plans = await self._workspaces.plan_wave(
            loop_id,
            tuple(item.directive_id for item in directives),
            concurrency,
        )
        by_directive = {item.directive_id: item for item in directives}
        planned = {item.directive_id for item in plans}
        await self._release_claims(set(by_directive) - planned, "workspace_capacity")
        run_ids: list[str] = []
        remaining_claims = set(planned)
        for plan in plans:
            directive = by_directive[plan.directive_id]
            try:
                run_id = await self._launcher(
                    directive,
                    DelegatedDirectiveFactory.to_model_message(directive),
                    plan.slot_id,
                )
                await self._record_delivery(directive.directive_id, run_id)
                run_ids.append(run_id)
                remaining_claims.discard(directive.directive_id)
            except Exception as exc:
                await self._release_claims(remaining_claims, f"launch_failed:{type(exc).__name__}:{str(exc)[:500]}")
                raise
        return tuple(run_ids)

    async def _record_delivery(self, directive_id: str, run_id: str) -> None:
        async with self._sessions.begin() as session:
            directive = await session.get(LoopDirective, directive_id, with_for_update=True)
            if directive is None:
                raise LookupError("已启动 Run 的 Directive 不存在")
            if directive.status == "launching":
                directive.status = "launched"
                directive.launched_run_id = run_id
            if directive.lifecycle_state == "delivering":
                await self._lifecycle.transition(session, directive_id, "delivered", run_id=run_id)
                await self._lifecycle.transition(session, directive_id, "run_started", run_id=run_id)
                await self._causality.run_started(session, directive, run_id)

    async def _claim(self, loop_id: str, round_id: str, concurrency: int) -> tuple[LoopDirective, ...]:
        async with self._sessions.begin() as session:
            loop = await session.get(AgentLoop, loop_id)
            if loop is None:
                return ()
            directives = list(
                (
                    await session.scalars(
                        select(LoopDirective)
                        .where(
                            LoopDirective.loop_id == loop_id,
                            LoopDirective.round_id == round_id,
                            LoopDirective.status == "created",
                            LoopDirective.lifecycle_state == "authorized",
                            LoopDirective.attempt < LoopDirective.max_attempts,
                        )
                        .order_by(LoopDirective.created_at, LoopDirective.directive_id)
                        .limit(concurrency)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for directive in directives:
                directive.status = "launching"
                directive.attempt += 1
                directive.queued_reason = None
                await self._lifecycle.transition(session, directive.directive_id, "delivering")
            return tuple(directives)

    async def _release_claims(self, directive_ids: set[str], reason: str) -> None:
        if not directive_ids:
            return
        async with self._sessions.begin() as session:
            rows = list(
                (
                    await session.scalars(
                        select(LoopDirective)
                        .where(LoopDirective.directive_id.in_(directive_ids))
                        .with_for_update()
                    )
                ).all()
            )
            for row in rows:
                if row.status == "launching":
                    row.status = "blocked" if row.attempt >= row.max_attempts else "created"
                    row.queued_reason = "attempts_exhausted" if row.status == "blocked" else f"{reason}:attempt:{row.attempt + 1}"
                    await self._lifecycle.transition(
                        session,
                        row.directive_id,
                        "delivery_failed" if row.status == "blocked" else "authorized",
                        reason=reason if row.status == "blocked" else row.queued_reason,
                    )


class LoopRunWorkspaceBinder:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._leases = WorkspaceLeaseManager(sessions)

    async def bind(
        self,
        *,
        run_id: str,
        loop_id: str,
        body: Any,
        slot_id: str | None = None,
        directive_id: str | None = None,
    ) -> tuple[WorkspaceSlot, Any]:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, loop_id)
            run = await session.get(DesktopRun, run_id)
            if loop is None or run is None or run.loop_id != loop_id:
                raise LookupError("Loop Run 执行身份不存在")
            slot = await self._slot(session, loop, slot_id)
            intent = WorkspaceIntentDeriver.derive(run.equipment or {})
        lease = await self._leases.acquire(
            WorkspaceLeaseRequest(slot_id=slot.slot_id, run_id=run_id, mode=intent.mode)
        )
        try:
            await self._persist_anchor(run_id, directive_id, loop.workspace_id, slot, lease)
        except Exception:
            await self._leases.release(lease.lease_id, lease.fencing_token)
            raise
        body.context["workspace_lease"] = {
            "lease_id": lease.lease_id,
            "fencing_token": lease.fencing_token,
            "slot_id": slot.slot_id,
            "mode": lease.mode.value if hasattr(lease.mode, "value") else str(lease.mode),
            "ttl_seconds": 120,
        }
        body.context["workspace_lease_guard"] = self._leases.assert_valid
        body.context["workspace_lease_renew"] = self._leases.renew
        return slot, lease

    async def execution_root(self, loop_id: str, slot_id: str | None = None) -> str:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, loop_id)
            if loop is None:
                raise LookupError("Loop 不存在")
            return (await self._slot(session, loop, slot_id)).root_path

    @staticmethod
    async def _slot(session: AsyncSession, loop: AgentLoop, slot_id: str | None) -> WorkspaceSlot:
        query = select(WorkspaceSlot).where(
            WorkspaceSlot.workspace_id == loop.workspace_id,
            WorkspaceSlot.lifecycle == "active",
        )
        query = query.where(WorkspaceSlot.slot_id == slot_id) if slot_id else query.where(WorkspaceSlot.kind == "authoritative")
        slot = await session.scalar(query)
        if slot is None:
            raise LookupError("Loop 可用 workspace slot 不存在")
        if slot.kind == "isolated" and slot.owner_loop_id != loop.loop_id:
            raise LookupError("隔离 workspace slot 不属于当前 Loop")
        return slot

    async def _persist_anchor(self, run_id, directive_id, workspace_id, slot, lease) -> None:
        async with self._sessions.begin() as session:
            run = await session.get(DesktopRun, run_id, with_for_update=True)
            if run is None or run.status != "pending":
                raise LookupError("待绑定的 Loop Run 已失效")
            run.workspace_anchor = {
                "workspace_id": workspace_id,
                "slot_id": slot.slot_id,
                "workspace_revision": slot.revision,
                "fingerprint": slot.current_fingerprint,
                "lease_id": lease.lease_id,
                "fencing_token": lease.fencing_token,
                "mode": lease.mode.value if hasattr(lease.mode, "value") else str(lease.mode),
            }
            anchor = await session.get(RunExecutionAnchor, run_id, with_for_update=True)
            if anchor is None:
                anchor = RunExecutionAnchor(
                    run_id=run_id,
                    context_revision_id=run.context_revision_id,
                    checkpoint_id=run.context_checkpoint_id,
                    slot_id=slot.slot_id,
                    lease_id=lease.lease_id,
                    directive_id=directive_id,
                    observed_workspace_revision=slot.revision,
                    observed_fingerprint=slot.current_fingerprint,
                )
                session.add(anchor)
            else:
                anchor.slot_id = slot.slot_id
                anchor.lease_id = lease.lease_id
                anchor.directive_id = directive_id
                anchor.observed_workspace_revision = slot.revision
                anchor.observed_fingerprint = slot.current_fingerprint


class DesktopDirectiveLaunchPort:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], desktop_service) -> None:
        self._sessions = sessions
        self._desktop = desktop_service
        self._workspace = LoopRunWorkspaceBinder(sessions)
        self._lifecycle = DirectiveLifecycleRepository()

    async def __call__(self, directive: LoopDirective, message, slot_id: str) -> str:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, directive.loop_id)
            if loop is None:
                raise LookupError("directive 所属 Loop 不存在")
            equipment = loop.equipment or {}
            prior_attempts = int(
                await session.scalar(
                    select(func.count()).select_from(DesktopRun).where(
                        DesktopRun.directive_id == directive.directive_id
                    )
                ) or 0
            )
        run_id = uuid.uuid4().hex
        prepared = await self._desktop.start_main_run(
            task_id=directive.target_context_id,
            message=message.content,
            model_name=equipment.get("model_name"),
            permissions=list(equipment.get("permissions") or ["read"]),
            skills=list(equipment.get("skills") or []),
            memory_ids=list(equipment.get("memory_ids") or []),
            access_mode=equipment.get("access_mode"),
            run_identity={
                "run_id": run_id,
                "message_id": directive.message_id,
                "origin": "delegated_patrol",
                "directive_id": directive.directive_id,
                "loop_id": directive.loop_id,
                "round_id": directive.round_id,
                "action_id": directive.action_id,
                "idempotency_key": f"directive:{directive.directive_id}:attempt:{prior_attempts + 1}",
            },
            execution_workspace_path=await self._workspace.execution_root(directive.loop_id, slot_id),
        )
        try:
            activity_bridge = LoopRunActivityBridge(
                self._desktop.bridge,
                self._sessions,
                loop_id=directive.loop_id,
                context_id=directive.target_context_id,
                run_id=run_id,
                correlation_id=directive.correlation_id,
                anchor_message_id=directive.message_id,
            )
            await self._workspace.bind(
                run_id=run_id,
                loop_id=directive.loop_id,
                body=prepared.body,
                slot_id=slot_id,
                directive_id=directive.directive_id,
            )
            async with self._sessions.begin() as session:
                current = await session.get(LoopDirective, directive.directive_id, with_for_update=True)
                current_loop = await session.get(AgentLoop, directive.loop_id, with_for_update=True)
                run = await session.get(DesktopRun, run_id, with_for_update=True)
                grant = await session.scalar(
                    select(LoopDelegationGrant).where(
                        LoopDelegationGrant.loop_id == directive.loop_id,
                        LoopDelegationGrant.revision == directive.grant_revision,
                        LoopDelegationGrant.status == "active",
                    )
                )
                if (
                    current is None
                    or current.status != "launching"
                    or current_loop is None
                    or current_loop.status != "running"
                    or current_loop.authority_revision != directive.grant_revision
                    or current_loop.goal_revision != directive.goal_revision
                    or grant is None
                    or run is None
                    or run.status != "pending"
                ):
                    raise LookupError("delegated directive 在启动前已被用户权力或新状态取代")
                record = await execute_prepared_run(
                    prepared.body,
                    prepared.thread_id,
                    RunExecutionResources(
                        bridge=activity_bridge,
                        run_manager=self._desktop.run_manager,
                        checkpointer=self._desktop.checkpointer,
                        store=self._desktop.store,
                        app_config=self._desktop.app_config,
                    ),
                    prepared.agent_factory,
                )
                current.status = "launched"
                current.launched_run_id = record.run_id
            activity_task = asyncio.create_task(self._finish_activity(record, activity_bridge), name=f"loop-run-activity-flush:{record.run_id}")
            setattr(record, "loop_activity_task", activity_task)
            self._desktop.attach_run_sync(record)
            return record.run_id
        except Exception as exc:
            self._desktop.run_manager.cancel(run_id, action="interrupt")
            if "record" in locals() and record.task is not None:
                await asyncio.gather(record.task, return_exceptions=True)
            if "activity_bridge" in locals():
                await activity_bridge.close()
            await RunLifecycleFinalizer(self._sessions, self._desktop.checkpointer).abort_prepared(run_id, str(exc))
            raise

    @staticmethod
    async def _finish_activity(record, activity_bridge: LoopRunActivityBridge) -> None:
        try:
            if record.task is not None:
                await record.task
        finally:
            await activity_bridge.close()
