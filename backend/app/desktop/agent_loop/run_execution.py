r"""本文件对外提供 LoopRunExecutionBoundary。

输入为已持久化的 Loop Run 身份、规划工作区与通用 Run 执行资源；输出为经当前授权复检后启动的 RunRecord 和可冲刷的实时活动桥。
具体工作流为装配前及启动时读取同一 Run 计划，锁定 Loop、Directive 和当前 Context，核对授权与 revision；注入实际凭据值供活动桥脱敏测试标题，实际执行启动成功后记录 directive.run_started，随后在 Run 结束时冲刷 journal。
示例：`record = await boundary.start(assembly, resources, execute_prepared_run)`。
直接用户消息也必须绑定正式 Directive，observed/交付/运行分别记录；Directive 存在时只由其因果记录器发布一次 Run 事件。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.agent_loop.directive_causality import DirectiveCausalityRecorder
from backend.app.desktop.agent_loop.directive_lifecycle import DirectiveLifecycleRepository
from backend.app.desktop.agent_loop.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.agent_loop.models import AgentLoop, LoopDelegationGrant, LoopDirective, LoopUserIntent
from backend.app.desktop.agent_loop.run_activity_bridge import LoopRunActivityBridge
from backend.app.desktop.agent_loop.user_run_events import LoopUserRunEventRecorder
from backend.app.desktop.models import DesktopRun, DesktopThread
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot
from backend.app.desktop.secret_redaction import configured_secret_values


class LoopRunExecutionBoundary:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._lifecycle = DirectiveLifecycleRepository()
        self._interventions = InterventionLifecycleRepository()
        self._causality = DirectiveCausalityRecorder()
        self._user_events = LoopUserRunEventRecorder()

    async def validate(self, run_id: str) -> None:
        async with self._sessions() as session:
            await self._identity(session, run_id, lock=False)

    async def start(self, assembly, resources, execute):
        activity = None
        record = None
        try:
            async with self._sessions.begin() as session:
                run, directive, intent = await self._identity(session, assembly.run_id, lock=True)
                activity = LoopRunActivityBridge(
                    resources.bridge,
                    self._sessions,
                    loop_id=run.loop_id,
                    context_id=run.task_id,
                    run_id=run.run_id,
                    correlation_id=self._correlation_id(directive, intent) or f"initial-run:{run.run_id}",
                    anchor_message_id=run.origin_message_id or (directive.message_id if directive is not None else ""),
                    secrets=configured_secret_values(resources.app_config),
                )
                adapted = type(resources)(
                    bridge=activity,
                    run_manager=resources.run_manager,
                    checkpointer=resources.checkpointer,
                    store=resources.store,
                    app_config=resources.app_config,
                )
                record = await execute(assembly.body, assembly.thread_id, adapted, assembly.agent_factory)
                if directive is not None:
                    if directive.lifecycle_state == "delivering":
                        await self._lifecycle.transition(session, directive.directive_id, "delivered", run_id=run.run_id)
                    if directive.lifecycle_state == "delivered":
                        await self._lifecycle.transition(session, directive.directive_id, "run_started", run_id=run.run_id)
                        await self._causality.run_started(session, directive, run.run_id)
                    directive.status = "launched"
                    directive.launched_run_id = run.run_id
                if intent is not None:
                    if intent.delivery_state == "observed" or (intent.intent_kind != "direct_message" and intent.delivery_state == "accepted"):
                        await self._interventions.transition(session, intent.intent_id, "delivered", run_id=run.run_id)
                    if intent.delivery_state == "delivered":
                        await self._interventions.transition(session, intent.intent_id, "run_started", run_id=run.run_id)
                if run.origin == "direct_user" and directive is None:
                    await self._user_events.started(session, intent, run)
            record.loop_activity_task = asyncio.create_task(
                self._finish(record, activity),
                name=f"loop-run-activity-flush:{assembly.run_id}",
            )
            return record
        except Exception:
            if record is not None:
                resources.run_manager.cancel(record.run_id, action="interrupt")
                if record.task is not None:
                    await asyncio.gather(record.task, return_exceptions=True)
            if activity is not None:
                await activity.close()
            raise

    async def _identity(self, session: AsyncSession, run_id: str, *, lock: bool):
        run = await session.get(DesktopRun, run_id, with_for_update=lock)
        if run is None or run.loop_id is None or run.status not in {"pending", "running"}:
            raise LookupError("Loop Run 执行身份已失效")
        loop = await session.get(AgentLoop, run.loop_id, with_for_update=lock)
        task = await session.get(DesktopThread, run.task_id, with_for_update=lock)
        if loop is None or loop.status != "running" or task is None or loop.current_round_id != run.round_id:
            raise LookupError("Loop 或 Context 已停止")
        if run.context_revision_id != task.current_revision_id:
            raise LookupError("Loop Run 目标 Context revision 已变化")
        slot_id = (run.workspace_anchor or {}).get("slot_id")
        if not slot_id:
            raise LookupError("Loop Run 缺少计划 workspace slot")
        slot = await session.get(WorkspaceSlot, slot_id, with_for_update=lock)
        if (
            slot is None or slot.lifecycle != "active" or slot.workspace_id != loop.workspace_id
            or (slot.kind == "isolated" and slot.owner_loop_id != loop.loop_id)
        ):
            raise LookupError("Loop Run 计划 workspace slot 已失效")
        directive = await session.get(LoopDirective, run.directive_id, with_for_update=lock) if run.directive_id else None
        intent = await session.get(LoopUserIntent, run.user_intent_id, with_for_update=lock) if run.user_intent_id else None
        if run.user_intent_id and (
            intent is None or intent.loop_id != run.loop_id or intent.target_context_id != run.task_id
            or intent.delivery_state not in ({"observed", "delivered", "run_started"} if intent.intent_kind == "direct_message" else {"accepted", "delivered", "run_started"})
            or (intent.resulting_run_id is not None and intent.resulting_run_id != run.run_id)
        ):
            raise LookupError("Loop 用户消息 intent 已失效")
        if run.directive_id:
            if (
                directive is None or directive.loop_id != run.loop_id
                or directive.target_context_id != run.task_id
                or directive.status not in {"launching", "launched"}
                or directive.lifecycle_state not in {"delivering", "delivered", "run_started"}
            ):
                raise LookupError("Loop directive 已失去启动权")
            if directive.launched_run_id is not None and directive.launched_run_id != run.run_id:
                raise LookupError("Loop directive 已绑定其他 Run")
            grant = await session.get(LoopDelegationGrant, directive.grant_id, with_for_update=lock)
            if (
                grant is None or grant.status != "active" or grant.loop_id != loop.loop_id
                or grant.revision != directive.grant_revision or loop.authority_revision != directive.grant_revision
                or loop.goal_revision != directive.goal_revision
                or run.context_revision_id != directive.target_context_revision_id
                or task.current_revision_id != directive.target_context_revision_id
                or (grant.expires_at is not None and grant.expires_at <= datetime.now(UTC))
            ):
                raise LookupError("Loop directive 授权或目标 revision 已失效")
        return run, directive, intent

    @staticmethod
    def _correlation_id(directive: LoopDirective | None, intent: LoopUserIntent | None) -> str | None:
        if directive is not None:
            return directive.correlation_id
        return intent.correlation_id if intent is not None else None

    @staticmethod
    async def _finish(record, activity: LoopRunActivityBridge) -> None:
        try:
            if record.task is not None:
                await record.task
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            await activity.close()
