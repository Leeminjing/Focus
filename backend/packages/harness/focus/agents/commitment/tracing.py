"""
本文件对外提供 emit_commitment_messages 函数，用于流式发布承诺层各角色的真实消息。

输入:
    actor — supervisor、worker 或 evaluator。
    stage / attempt — 当前业务阶段和审核轮次。
    messages — 该角色本次新增或当前完整的 BaseMessage 数组。

输出:
    None — 消息数组写入 LangGraph custom 流；非流式调用时静默跳过。

具体工作流:
    (1) 组装角色、阶段和真实 messages 数组。
    (2) 获取当前 LangGraph stream writer。
    (3) 由外层 middleware/worker 转发到现有 SSE events 通道。

示例:
    emit_commitment_messages(
        actor="worker",
        stage=2,
        messages=[AIMessage(content="...")],
    )
"""

from langchain_core.messages import BaseMessage
from langgraph.config import get_stream_writer


def _write_commitment_messages(payload: dict[str, object]) -> None:
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(payload)


def emit_commitment_messages(
    *,
    actor: str,
    stage: int,
    messages: list[BaseMessage],
    attempt: int | None = None,
) -> None:
    payload: dict[str, object] = {
        "type": "commitment_messages",
        "actor": actor,
        "stage": stage,
        "messages": [
            message.model_copy(update={"additional_kwargs": {}})
            for message in messages
        ],
    }
    if attempt is not None:
        payload["attempt"] = attempt
    _write_commitment_messages(payload)


def emit_commitment_trace(**_: object) -> None:
    """承诺层轨迹占位：真实执行过程经 commitment_messages 与阶段 ToolMessage 审计。"""
    return None
