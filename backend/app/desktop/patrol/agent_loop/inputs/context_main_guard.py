"""本文件对外提供 Workspace Patrol 统一输入的普通 Main Run 准入检查。

输入为事务和目标 Context；输出为允许普通 Main 输入或明确的 409 拒绝。
具体工作流为 HTTP 只读预检，持久准入通过注入的所有权策略锁定 Context 后再次校验，
避免在 Patrol 已管理的工作线上绕过统一输入。示例：`await ensure_context_main_input_allowed(session, context_id)`。
"""

from fastapi import HTTPException
from sqlalchemy import select

from backend.app.desktop.patrol.agent_loop.curation.curation_ownership import TERMINAL_LOOP_STATUSES
from backend.app.desktop.patrol.agent_loop.models import AgentLoop, LoopContextMembership
from backend.app.desktop.persistence.models import DesktopThread


async def ensure_context_main_input_allowed(session, context_id: str, *, lock: bool = False) -> None:
    if lock:
        await session.get(DesktopThread, context_id, with_for_update=True)
    owner = await session.scalar(select(AgentLoop.loop_id).join(
        LoopContextMembership, LoopContextMembership.loop_id == AgentLoop.loop_id,
    ).where(
        LoopContextMembership.context_id == context_id,
        LoopContextMembership.status.in_(("active", "paused")),
        AgentLoop.interaction_mode == "workspace_patrol",
        AgentLoop.status.not_in(TERMINAL_LOOP_STATUSES),
    ).limit(1))
    if owner is not None:
        raise HTTPException(409, "工作区 Patrol 请使用工作区统一输入")
