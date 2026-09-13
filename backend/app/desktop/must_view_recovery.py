"""本文件对外提供必看报告中断的恢复探测与载荷校验函数。

输入为桌面数据库会话、任务、LangGraph checkpointer 和用户 resume 载荷；输出为与压缩恢复
一致的 processing/resumable/orphaned 投影，或经过校验的 retry/cancel Command 载荷。
具体工作流为读取主图 checkpoint 最新 type=must_view_report 的 interrupt，再结合最近主运行
状态判断可恢复性；校验器拒绝未知动作和与当前 interrupt 类型不匹配的载荷。

示例：recovery = await must_view_recovery_payload(session, task, checkpointer)。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

logger = logging.getLogger(__name__)


async def must_view_recovery_payload(
    session: Any, task: Any, checkpointer: Any
) -> dict[str, Any] | None:
    config = {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
    try:
        checkpoint = await checkpointer.aget_tuple(config)
    except Exception:
        logger.warning("读取必看报告 checkpoint 失败: thread_id=%s", task.thread_id, exc_info=True)
        return None
    if checkpoint is None:
        return None
    request = _checkpoint_request(checkpoint)
    if request is None:
        return None
    from backend.app.desktop.models import DesktopRun

    latest = await session.scalar(
        select(DesktopRun)
        .where(
            DesktopRun.task_id == task.task_id,
            DesktopRun.agent_id == f"main:{task.task_id}",
        )
        .order_by(DesktopRun.created_at.desc())
    )
    if latest is not None and latest.status in {"pending", "running"}:
        status = "processing"
    elif latest is not None and latest.status == "interrupted":
        status = "resumable"
    else:
        status = "orphaned"
    return {"status": status, "request": request}


def validate_must_view_resume(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("type") != "must_view_report":
        raise HTTPException(422, "必看报告恢复载荷类型错误")
    decision = value.get("decision")
    if decision not in {"retry", "cancel"}:
        raise HTTPException(422, "必看报告仅支持 retry 或 cancel")
    return {"type": "must_view_report", "decision": str(decision)}


def _checkpoint_request(checkpoint: Any) -> dict[str, Any] | None:
    pending_writes = list(getattr(checkpoint, "pending_writes", None) or [])
    for _task_id, channel, raw_value in reversed(pending_writes):
        if channel != "__interrupt__":
            continue
        values = raw_value if isinstance(raw_value, (list, tuple)) else [raw_value]
        for item in reversed(values):
            payload = getattr(item, "value", item)
            if isinstance(payload, dict) and payload.get("type") == "must_view_report":
                return dict(payload)
    return None
