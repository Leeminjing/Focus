"""本文件对外提供未决人工中断的统一投影：按（会话标识 + 执行命名空间）枚举并分类。

对外提供:
    pending_interrupt_payload(checkpoint, payload_type) — 从检查点取指定类型的最新中断载荷
    main_run_status(session, task) — 主执行最近一次运行归一的收敛状态
    main_pending_interrupt(session, task, checkpointer, payload_type) — 主执行上的未决中断
    main_pending_kinds(session, task, checkpointer, payload_types) — 主执行上全部未决中断的类型

输入:
    checkpoint: Any — LangGraph 检查点元组（含 pending_writes）
    payload_type / payload_types: str / 序列 — 中断载荷的类型标记
    session / task / checkpointer — 数据库会话、桌面线程与检查点持久化器

输出:
    payload → dict | None
    main_run_status → "processing" | "resumable" | "orphaned"
    main_pending_interrupt → {"status": str, "request": dict} | None
    main_pending_kinds → list[str]（按传入顺序，仅含确实未决者）

具体工作流:
    (1) 只读主图命名空间（checkpoint_ns=""）——它是主执行身份的一部分；后台执行主体
        （swarm / patrol / spatial）的中断位于各自命名空间，不属于主执行，因此不会阻塞主运行
    (2) 在 pending_writes 的 __interrupt__ 通道上倒序取最新一条匹配类型的载荷
    (3) 用主执行最近一次运行的状态把未决归一为 processing / resumable / orphaned，
        使「正在处理」与「父图已无法恢复」在调用方是两种不同的拒绝

示例:
    pending = await main_pending_interrupt(session, task, checkpointer, APPROVAL_TYPE)
    kinds = await main_pending_kinds(session, task, checkpointer, (COMPRESSION_TYPE, APPROVAL_TYPE))
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from backend.app.desktop.models import DesktopRun

logger = logging.getLogger(__name__)

MAIN_CHECKPOINT_NAMESPACE = ""
"""主执行的检查点命名空间；后台执行主体各自另有一个，互不阻塞。"""

_ACTIVE_STATUSES = frozenset({"pending", "running"})
_RESUMABLE_STATUS = "interrupted"
_INTERRUPT_CHANNEL = "__interrupt__"


def pending_interrupt_payload(checkpoint: Any, payload_type: str) -> dict[str, Any] | None:
    """从检查点的中断通道倒序取出指定类型的最新载荷。"""
    pending_writes = list(getattr(checkpoint, "pending_writes", None) or [])
    for _task_id, channel, raw_value in reversed(pending_writes):
        if channel != _INTERRUPT_CHANNEL:
            continue
        values = raw_value if isinstance(raw_value, (list, tuple)) else [raw_value]
        for item in reversed(values):
            payload = getattr(item, "value", item)
            if isinstance(payload, dict) and payload.get("type") == payload_type:
                return dict(payload)
    return None


async def main_run_status(session: Any, task: Any) -> str:
    """主执行最近一次运行归一的收敛状态。"""
    latest = await session.scalar(
        select(DesktopRun)
        .where(
            DesktopRun.task_id == task.task_id,
            DesktopRun.agent_id == f"main:{task.task_id}",
        )
        .order_by(DesktopRun.created_at.desc())
    )
    if latest is not None and latest.status in _ACTIVE_STATUSES:
        return "processing"
    if latest is not None and latest.status == _RESUMABLE_STATUS:
        return "resumable"
    return "orphaned"


async def main_pending_interrupt(
    session: Any, task: Any, checkpointer: Any, payload_type: str
) -> dict[str, Any] | None:
    """主执行身份上指定类型的未决中断；只读主图命名空间。"""
    checkpoint = await _main_checkpoint(task, checkpointer)
    if checkpoint is None:
        return None
    request = pending_interrupt_payload(checkpoint, payload_type)
    if request is None:
        return None
    return {"status": await main_run_status(session, task), "request": request}


async def main_pending_kinds(
    session: Any, task: Any, checkpointer: Any, payload_types: Any
) -> list[str]:
    """主执行身份上全部未决中断的类型，按传入顺序返回。"""
    checkpoint = await _main_checkpoint(task, checkpointer)
    if checkpoint is None:
        return []
    return [kind for kind in payload_types if pending_interrupt_payload(checkpoint, kind) is not None]


async def _main_checkpoint(task: Any, checkpointer: Any) -> Any | None:
    """读取主图检查点；读取失败按无未决处理并留下告警。"""
    config = {
        "configurable": {
            "thread_id": task.thread_id,
            "checkpoint_ns": MAIN_CHECKPOINT_NAMESPACE,
        }
    }
    try:
        return await checkpointer.aget_tuple(config)
    except Exception:
        logger.warning("读取主图 checkpoint 失败: thread_id=%s", task.thread_id, exc_info=True)
        return None
