r"""本文件对外提供兼容的 ContextProjection 与 compile_context_messages 入口。

输入为用户定义或冻结 checkpoint 的消息字典列表及可选 ProtocolRepairContext；输出为共享协议模块生成的 authored、execution、
repair manifest、issues、valid/repaired/approval_required 状态与稳定 definition_hash/projection_hash。具体工作流为把所有检查和修复委托给 context_protocol，保证 definition 与 Run settlement
使用同一规则。示例：`result = compile_context_messages(messages, ProtocolRepairContext.interrupted(run_id, call_ids=("call-1",)))`。
"""

from __future__ import annotations

from typing import Any

from backend.app.desktop.context_protocol import (
    ContextProtocolProjection,
    ProtocolProjectionStatus,
    ProtocolRepairContext,
    compile_protocol_messages,
)

ContextProjection = ContextProtocolProjection
ProjectionStatus = ProtocolProjectionStatus


def compile_context_messages(
    messages: list[dict[str, Any]],
    repair_context: ProtocolRepairContext | None = None,
) -> ContextProjection:
    return compile_protocol_messages(messages, repair_context)


__all__ = [
    "ContextProjection",
    "ProjectionStatus",
    "ProtocolRepairContext",
    "compile_context_messages",
]
