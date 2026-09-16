r"""本文件对外提供主 Agent 最近执行身份的只读解析函数。

输入为数据库会话与 DesktopThread；输出为最近主 Run 以及由其持久化字段还原的
MainExecutionIdentity。具体工作流为按创建时间读取 `main:{task_id}` 的最后一次 Run，优先采用
该 Run 的 execution_thread_id、checkpoint_ns 与 context_revision_id；历史数据缺少字段时才回退
DesktopThread 的根执行身份。示例：`identity = await resolve_main_execution_identity(session, task)`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from backend.app.desktop.models import DesktopRun


@dataclass(frozen=True, slots=True)
class MainExecutionIdentity:
    thread_id: str
    checkpoint_ns: str
    context_revision_id: str | None


async def latest_main_run(session: Any, task: Any) -> DesktopRun | None:
    if session is None:
        return None
    return await session.scalar(
        select(DesktopRun)
        .where(
            DesktopRun.task_id == task.task_id,
            DesktopRun.agent_id == f"main:{task.task_id}",
        )
        .order_by(DesktopRun.created_at.desc())
    )


async def resolve_main_execution_identity(
    session: Any, task: Any
) -> MainExecutionIdentity:
    latest = await latest_main_run(session, task)
    return identity_from_main_run(task, latest)


def identity_from_main_run(task: Any, run: DesktopRun | None) -> MainExecutionIdentity:
    return MainExecutionIdentity(
        thread_id=(
            str(getattr(run, "execution_thread_id", ""))
            if run is not None and getattr(run, "execution_thread_id", None)
            else str(task.thread_id)
        ),
        checkpoint_ns=(
            str(getattr(run, "checkpoint_ns", "") or "")
            if run is not None
            else ""
        ),
        context_revision_id=(
            getattr(run, "context_revision_id", None)
            if run is not None and getattr(run, "context_revision_id", None)
            else getattr(task, "current_revision_id", None)
        ),
    )
