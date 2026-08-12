"""本文件对外提供 select_checkpoint_base，为桌面主任务选择可继续的 checkpoint 基线。

输入为 LangGraph checkpointer、thread_id 与 checkpoint namespace；输出为 None（沿用最新）、
最近合法祖先 checkpoint_id，或 EMPTY_CHECKPOINT_ID（从空历史启动）。具体工作流为：先读取
最新 checkpoint 并复用 serialize_message/validate_messages 校验；仅当非法时倒序遍历同线程
checkpoint，返回首个合法祖先，全程不写数据库。示例：
`checkpoint_id = await select_checkpoint_base(checkpointer, thread_id)`。
"""

from typing import Any

from focus.runtime.runs.events import serialize_message, validate_messages


EMPTY_CHECKPOINT_ID = "00000000-0000-0000-0000-000000000000"


def _is_valid_checkpoint(checkpoint_tuple: Any) -> bool:
    values = checkpoint_tuple.checkpoint.get("channel_values", {})
    messages = [serialize_message(message) for message in values.get("messages", [])]
    try:
        validate_messages(messages)
    except ValueError:
        return False
    return True


async def select_checkpoint_base(
    checkpointer: Any, thread_id: str, checkpoint_ns: str = ""
) -> str | None:
    """选择主 run 的 checkpoint_id；None 表示无需覆盖默认最新 checkpoint。"""
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}}
    latest = await checkpointer.aget_tuple(config)
    if latest is None or _is_valid_checkpoint(latest):
        return None
    async for candidate in checkpointer.alist(config):
        if _is_valid_checkpoint(candidate):
            return candidate.config.get("configurable", {}).get("checkpoint_id")
    return EMPTY_CHECKPOINT_ID
