r"""本文件对外提供旧 Context Loop 的启动前退役收敛。

输入为已迁移数据库的 session factory；输出为本次停止的旧 Loop identity。
具体工作流为逐个锁定非终态 context_loop、撤销旧控制版本并复用终态生命周期，
取消执行、撤权和释放策展所有权；历史内容与账务保留，Workspace Patrol 不受影响。
示例：`await retire_context_loops(sessions)`，在正常 Gateway 启动运行器之前调用。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.desktop.patrol.agent_loop.curation.curation_ownership import TERMINAL_LOOP_STATUSES
from backend.app.desktop.patrol.agent_loop.governance.intervention_lifecycle import InterventionLifecycleRepository
from backend.app.desktop.patrol.agent_loop.lifecycle.terminal_lifecycle import LoopTerminalLifecycle
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopUserIntent


async def retire_context_loops(sessions: async_sessionmaker[AsyncSession]) -> tuple[str, ...]:
    """Retire active legacy owners before any recovered work can be dispatched."""
    async with sessions() as session:
        loop_ids = tuple(await session.scalars(select(AgentLoop.loop_id).where(
            AgentLoop.interaction_mode == "context_loop",
            AgentLoop.status.not_in(TERMINAL_LOOP_STATUSES),
        ).order_by(AgentLoop.loop_id)))
    lifecycle = LoopTerminalLifecycle()
    retired: list[str] = []
    for loop_id in loop_ids:
        async with sessions.begin() as session:
            loop = await session.get(AgentLoop, loop_id, with_for_update=True)
            if loop is None or loop.status in TERMINAL_LOOP_STATUSES:
                continue
            loop.revision += 1
            loop.authority_revision += 1
            await lifecycle.finalize(session, loop, "stopped", "context_loop_retired")
            pending_inputs = tuple(await session.scalars(select(LoopUserIntent).where(
                LoopUserIntent.loop_id == loop_id,
                LoopUserIntent.intent_kind == "direct_message",
                LoopUserIntent.delivery_state.in_(("submitted", "accepted", "observed", "delivered", "run_started")),
            ).with_for_update()))
            for intent in pending_inputs:
                await InterventionLifecycleRepository().transition(
                    session, intent.intent_id, "cancelled", reason="context_loop_retired",
                )
            retired.append(loop_id)
    return tuple(retired)
