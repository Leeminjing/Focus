"""本文件对外提供必看报告中断的恢复探测与载荷校验函数。

输入为桌面数据库会话、任务、LangGraph checkpointer 和用户 resume 载荷；输出为与压缩恢复
一致的 processing/resumable/orphaned 投影，或经过校验的 retry/cancel Command 载荷。
具体工作流为复用统一未决中断投影，按最近主 Run 的真实执行身份读取 type=must_view_report
的 interrupt 并判断可恢复性；校验器拒绝未知动作和与当前 interrupt 类型不匹配的载荷。

示例：recovery = await must_view_recovery_payload(session, task, checkpointer)。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from backend.app.desktop.pending_interrupts import main_pending_interrupt


MUST_VIEW_INTERRUPT_TYPE = "must_view_report"


async def must_view_recovery_payload(
    session: Any, task: Any, checkpointer: Any
) -> dict[str, Any] | None:
    return await main_pending_interrupt(
        session, task, checkpointer, MUST_VIEW_INTERRUPT_TYPE
    )


def validate_must_view_resume(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("type") != "must_view_report":
        raise HTTPException(422, "必看报告恢复载荷类型错误")
    decision = value.get("decision")
    if decision not in {"retry", "cancel"}:
        raise HTTPException(422, "必看报告仅支持 retry 或 cancel")
    return {"type": "must_view_report", "decision": str(decision)}
