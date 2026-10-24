"""本文件对外提供 start_loop_run 的隔离测试启动端口。

输入为已交付的持久 pending Loop Run；输出为经生产 LoopRunExecutionBoundary 启动并标记 running 的 Run identity。
具体工作流为绑定测试工作区计划，调用真实启动边界校验授权并记录用户/Directive 生命周期，等待活动桥收口。
示例：await start_loop_run(sessions, run_id)。仅 Agent 执行函数使用无外部模型的成功替身，生命周期不手工补写。
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from backend.app.desktop.patrol.agent_loop.models import AgentLoop
from backend.app.desktop.patrol.agent_loop.execution.run_execution import LoopRunExecutionBoundary
from backend.app.desktop.persistence.models import DesktopRun, DesktopThread
from backend.app.desktop.runs.orchestration.executor import RunExecutionResources
from backend.app.desktop.workspaces.coordination.models import WorkspaceSlot


async def freeze_patrol_intent(sessions, intent):
    import uuid
    from backend.app.desktop.patrol.agent_loop.models import LoopObservation, LoopRound, LoopContextMembership
    from backend.app.desktop.patrol.agent_loop.schemas import LoopObservationEnvelope, ContextPermission
    from backend.app.desktop.patrol.agent_loop.observations.builder import observation_hash
    from backend.app.desktop.patrol.agent_loop.execution.directive_equipment import resolve_context_equipment

    async with sessions.begin() as session:
        loop = await session.get(AgentLoop, intent.loop_id, with_for_update=True)
        round_row = await session.get(LoopRound, intent.round_id, with_for_update=True)
        row = await session.get(LoopObservation, round_row.observation_id) if round_row.observation_id else None
        contexts = tuple(await session.scalars(select(LoopContextMembership.context_id).where(
            LoopContextMembership.loop_id == loop.loop_id, LoopContextMembership.status == "active")))
        permissions = tuple([ContextPermission(context_id=context_id,
            access_mode=(await resolve_context_equipment(session, loop, context_id))["access_mode"]) for context_id in contexts])
        permissions += tuple(ContextPermission(context_id=f"action:{index}", access_mode="read-only")
            for index, action in enumerate(intent.actions)
            if action.action in {"create_lane", "update_lane", "merge_contexts", "spawn_context", "recover_context"})
        if row is None:
            identity = uuid.uuid4().hex
            envelope = LoopObservationEnvelope(observation_id=identity, generation=1,
                decision_inputs_ref=identity, loop_id=loop.loop_id, round_id=round_row.round_id,
                loop_revision=intent.loop_revision, goal_revision=intent.goal_revision,
                authority_revision=intent.grant_revision, observed_frontier_hash=intent.observed_frontier_hash,
                goal={}, grant={"grant_id": intent.grant_id, "holder_id": intent.holder_id},
                portfolio_frontier=tuple([{"context_id": context_id,
                    "revision_id": (await session.get(DesktopThread, context_id)).current_revision_id} for context_id in contexts]),
                workspace={"revision": intent.observed_workspace_revision}, budget={})
            row = LoopObservation(observation_id=identity, loop_id=loop.loop_id, round_id=round_row.round_id,
                generation=1, envelope=envelope.model_dump(mode="json"), envelope_hash=observation_hash(envelope))
            session.add(row)
            await session.flush()
            round_row.observation_id = identity
        return intent.model_copy(update={"observation_id": row.observation_id,
            "observation_generation": row.generation, "observed_envelope_hash": row.envelope_hash,
            "context_permissions": intent.context_permissions or permissions})


async def commit_patrol(sessions, intent):
    from backend.app.desktop.patrol.agent_loop.kernel import LoopKernel

    return await LoopKernel(sessions).commit(await freeze_patrol_intent(sessions, intent))


async def start_loop_run(sessions, run_id):
    async with sessions.begin() as session:
        run = await session.get(DesktopRun, run_id)
        loop = await session.get(AgentLoop, run.loop_id)
        assert run.status == "pending"
        if not (run.workspace_anchor or {}).get("slot_id"):
            slot = await session.scalar(
                select(WorkspaceSlot).where(
                    WorkspaceSlot.workspace_id == loop.workspace_id,
                    WorkspaceSlot.kind == "authoritative",
                    WorkspaceSlot.lifecycle == "active",
                )
            )
            run.workspace_anchor = {"slot_id": slot.slot_id}
        assembly = SimpleNamespace(
            run_id=run_id,
            thread_id=run.execution_thread_id,
            body=None,
            agent_factory=None,
        )
    resources = RunExecutionResources(
        bridge=None, run_manager=None, checkpointer=None, store=None, app_config=None
    )

    async def execute(body, thread_id, resources, factory):
        return SimpleNamespace(run_id=run_id, task=None)

    record = await LoopRunExecutionBoundary(sessions).start(
        assembly, resources, execute
    )
    await record.loop_activity_task
    async with sessions.begin() as session:
        run = await session.get(DesktopRun, run_id)
        run.status = "running"
    return run_id
