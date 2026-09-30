"""本文件对外提供 start_loop_run 的隔离测试启动端口。

输入为已交付的持久 pending Loop Run；输出为经生产 LoopRunExecutionBoundary 启动并标记 running 的 Run identity。
具体工作流为绑定测试工作区计划，调用真实启动边界校验授权并记录用户/Directive 生命周期，等待活动桥收口。
示例：await start_loop_run(sessions, run_id)。仅 Agent 执行函数使用无外部模型的成功替身，生命周期不手工补写。
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from backend.app.desktop.agent_loop.models import AgentLoop
from backend.app.desktop.agent_loop.run_execution import LoopRunExecutionBoundary
from backend.app.desktop.models import DesktopRun
from backend.app.desktop.run_orchestration.executor import RunExecutionResources
from backend.app.desktop.workspace_coordination.models import WorkspaceSlot


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
