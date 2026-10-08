r"""本文件对外提供 LoopWaveDispatcher、LoopRunWorkspaceBinder、DesktopDirectiveLaunchPort 与 DirectiveLaunchPort。

输入为已授权 Directive、规划出的 workspace slot 与 Run 请求；输出为持久的 Run/dispatch 交付和启动时的 workspace lease。
具体工作流为 Dispatcher 认领 directive 并分配 slot，LaunchPort 将计划随 Run 准入原子提交，通用 durable worker 在启动边界调用 Binder 获取 lease；交付状态与实际启动状态分别记录。
启动边界可先于 launcher 返回推进 Directive；交付确认仍须幂等收口 Expansion 的 dispatched 状态，不以 delivering 作为唯一入口。
认领、交付确认与释放统一先锁 Loop，再锁 Directive，与 Run 启动和控制事务保持相同顺序。
每次工具效果检查同时复核持久 Loop 控制及 workspace lease，暂停或撤权后拒绝旧执行。
资源等待释放认领且不消耗启动 attempt；准备失败释放整批认领，交付失败只计入真正尝试的 Directive，其余继续排队。
示例：`run_ids = await dispatcher.dispatch(loop_id, round_id)`。
用户 Directive 从不可变请求交付原始 HumanMessage、材料和焦点，服务端当前角色解析装备，固定 direct_user/user_intent_id；通用 dispatch 与启动边界共同幂等确认交付。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.context_expansion.repository import (
    ContextExpansionRepository,
)
from backend.app.desktop.agent_loop.directive_lifecycle import (
    DirectiveLifecycleRepository,
)
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopDirective, LoopRound
from backend.app.desktop.agent_loop.provenance import DelegatedDirectiveFactory
from backend.app.desktop.agent_loop.user_message_delivery import LoopUserMessageDelivery
from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.agent_loop.workspace_planning import WorkspaceRunPlanner
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.workspace_coordination.leases import WorkspaceLeaseManager
from backend.app.desktop.workspace_coordination.models import (
    RunExecutionAnchor,
    WorkspaceSlot,
)
from backend.app.desktop.workspace_coordination.schemas import (
    WorkspaceIntentDeriver,
    WorkspaceLeaseRequest,
)


class DirectiveLaunchPort(Protocol):
    async def __call__(self, directive: LoopDirective, message, slot_id: str) -> str: ...


class LoopWaveDispatcher:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], launcher: DirectiveLaunchPort) -> None:
        self._sessions = sessions
        self._launcher = launcher
        self._workspaces = WorkspaceRunPlanner(sessions)
        self._lifecycle = DirectiveLifecycleRepository()
        self._expansions = ContextExpansionRepository()

    async def dispatch(self, loop_id: str, round_id: str, concurrency: int) -> tuple[str, ...]:
        directives = await self._claim(loop_id, round_id, concurrency)
        if not directives:
            return ()
        identities = {item.directive_id for item in directives}
        try:
            plans = await self._workspaces.plan_wave(loop_id, tuple(identities), concurrency)
        except BaseException as exc:
            await self._release_claims(loop_id, identities, f"workspace_prepare_failed:{type(exc).__name__}",
                                       consume_attempt=isinstance(exc, Exception))
            raise
        by_directive = {item.directive_id: item for item in directives}
        planned = {item.directive_id for item in plans}
        await self._release_claims(loop_id, set(by_directive) - planned, "workspace_capacity", consume_attempt=False)
        run_ids: list[str] = []
        remaining_claims = set(planned)
        for plan in plans:
            directive = by_directive[plan.directive_id]
            try:
                async with self._sessions() as session:
                    message = await LoopUserMessageDelivery.model_message(session, directive) if directive.origin_kind == "direct_user" else DelegatedDirectiveFactory.to_model_message(directive)
                run_id = await self._launcher(
                    directive,
                    message,
                    plan.slot_id,
                )
                await self._record_delivery(directive.directive_id, run_id)
                run_ids.append(run_id)
                remaining_claims.discard(directive.directive_id)
            except BaseException as exc:
                await self._release_claims(loop_id, {directive.directive_id},
                    f"launch_failed:{type(exc).__name__}:{str(exc)[:500]}", consume_attempt=isinstance(exc, Exception))
                await self._release_claims(loop_id, remaining_claims - {directive.directive_id},
                                           "wave_delivery_pending", consume_attempt=False)
                raise
        return tuple(run_ids)

    async def _record_delivery(self, directive_id: str, run_id: str) -> None:
        async with self._sessions.begin() as session:
            identity = await session.get(LoopDirective, directive_id)
            if identity is None:
                raise LookupError("已启动 Run 的 Directive 不存在")
            await session.get(AgentLoop, identity.loop_id, with_for_update=True)
            directive = await session.get(LoopDirective, directive_id, with_for_update=True, populate_existing=True)
            if directive is None:
                raise LookupError("已启动 Run 的 Directive 不存在")
            if directive.status == "launching":
                directive.status = "launched"
                directive.launched_run_id = run_id
            if directive.lifecycle_state == "delivering":
                await self._lifecycle.transition(session, directive_id, "delivered", run_id=run_id)
            if directive.origin_kind == "direct_user" and directive.launched_run_id == run_id:
                from backend.app.desktop.agent_loop.models import LoopUserIntent

                intent = await session.get(LoopUserIntent, directive.correlation_id, with_for_update=True)
                if intent is not None and intent.delivery_state == "observed":
                    await InterventionLifecycleRepository().transition(session, intent.intent_id, "delivered", run_id=run_id)
            if directive.launched_run_id == run_id and directive.lifecycle_state in {"delivered", "run_started", "settled", "failed"}:
                expansion = await self._expansions.by_directive(session, directive.directive_id)
                if expansion is not None and expansion.state == "committed":
                    await self._expansions.transition(
                        session,
                        expansion.expansion_id,
                        "dispatched",
                        "Expansion Directive 已交付并启动 Context Run",
                        result={"run_id": run_id},
                    )

    async def _claim(self, loop_id: str, round_id: str, concurrency: int) -> tuple[LoopDirective, ...]:
        async with self._sessions.begin() as session:
            loop = await session.get(AgentLoop, loop_id, with_for_update=True)
            round_row = await session.get(LoopRound, round_id)
            if (loop is None or loop.status != "running" or loop.current_round_id != round_id
                    or round_row is None or round_row.status not in {"ready", "running"}):
                return ()
            grant = await session.scalar(select(LoopDelegationGrant).where(
                LoopDelegationGrant.loop_id == loop_id, LoopDelegationGrant.revision == loop.authority_revision,
                LoopDelegationGrant.status == "active"))
            if (grant is None or (grant.expires_at and grant.expires_at <= datetime.now(UTC))
                    or round_row.authority_revision != loop.authority_revision or round_row.goal_revision != loop.goal_revision):
                return ()
            active = int(await session.scalar(select(func.count()).select_from(DesktopRun).where(
                DesktopRun.loop_id == loop_id, DesktopRun.status.in_(("pending", "running")))) or 0)
            reserved = int(await session.scalar(select(func.count()).select_from(LoopDirective).where(
                LoopDirective.loop_id == loop_id, LoopDirective.status == "launching")) or 0)
            concurrency = min(concurrency, int((grant.budgets or {}).get("max_concurrent_runs") or concurrency) - active - reserved)
            if concurrency <= 0:
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
                            LoopDirective.grant_id == grant.grant_id,
                            LoopDirective.grant_revision == loop.authority_revision,
                            LoopDirective.goal_revision == loop.goal_revision,
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

    async def _release_claims(self, loop_id: str, directive_ids: set[str], reason: str, *, consume_attempt: bool = True) -> None:
        if not directive_ids:
            return
        async with self._sessions.begin() as session:
            await session.get(AgentLoop, loop_id, with_for_update=True)
            rows = list(
                (
                    await session.scalars(
                        select(LoopDirective)
                        .where(LoopDirective.loop_id == loop_id, LoopDirective.directive_id.in_(directive_ids))
                        .with_for_update()
                    )
                ).all()
            )
            for row in rows:
                if row.status == "launching":
                    if not consume_attempt:
                        row.attempt = max(0, row.attempt - 1)
                    row.status = "blocked" if row.attempt >= row.max_attempts else "created"
                    row.queued_reason = ("attempts_exhausted" if row.status == "blocked" else
                        f"{reason}:attempt:{row.attempt + 1}" if consume_attempt else row.queued_reason or reason)
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
        async def assert_execution(lease_id: str, token: int) -> None:
            from backend.app.desktop.agent_loop.execution_ownership import RunOwnershipPolicy

            async with self._sessions.begin() as session:
                current = await session.get(DesktopRun, run_id)
                if current is None:
                    raise LookupError("Run 已不存在")
                await RunOwnershipPolicy().assert_live(session, current)
            await self._leases.assert_valid(lease_id, token)

        body.context["workspace_lease_guard"] = assert_execution
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

    async def __call__(self, directive: LoopDirective, message, slot_id: str) -> str:
        async with self._sessions() as session:
            loop = await session.get(AgentLoop, directive.loop_id)
            if loop is None:
                raise LookupError("directive 所属 Loop 不存在")
            from backend.app.desktop.agent_loop.directive_equipment import resolve_directive_equipment

            equipment = await resolve_directive_equipment(session, loop, directive)
            from backend.app.desktop.agent_loop.models import LoopUserIntent

            user_intent = await session.get(LoopUserIntent, directive.correlation_id) if directive.origin_kind == "direct_user" else None
            user_request = (user_intent.request_payload.get("request") or {}) if user_intent else {}
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
            spatial_focus=user_request.get("spatial_focus"),
            material_inputs=self._material_inputs(user_request),
            attached_material_ids=user_request.get("attached_material_ids"),
            must_view_material_ids=user_request.get("must_view_material_ids") or [],
            run_identity={
                "run_id": run_id,
                "message_id": directive.message_id,
                "origin": "direct_user" if user_intent is not None else "delegated_patrol",
                "user_intent_id": user_intent.intent_id if user_intent is not None else None,
                "directive_id": directive.directive_id,
                "loop_id": directive.loop_id,
                "round_id": directive.round_id,
                "action_id": directive.action_id,
                "idempotency_key": f"directive:{directive.directive_id}:attempt:{prior_attempts + 1}",
            },
            execution_workspace_path=await self._workspace.execution_root(directive.loop_id, slot_id),
            execution_slot_id=slot_id,
            admit_only=True,
        )
        return str(prepared.payload.get("run_id") or run_id)

    @staticmethod
    def _material_inputs(request):
        from backend.app.desktop.run_materials import RunMaterialRequest

        items = request.get("material_inputs")
        return [RunMaterialRequest(material_id=item["material_id"], note=item.get("note")) for item in items] if items is not None else None
