"""Patrol 自动压缩的内部摘要生成与精确执行中断读取。

候选服务提供冻结原文，摘要模型仅生成候选文本，最终应用由 Kernel 授权。
本模块没有消息编辑 HTTP 入口，也不接受手工关键词剥离或原文恢复请求。
"""

from typing import Any

from focus.models import create_chat_model
from focus.models.lifecycle import model_client_scope

from backend.app.desktop.conversations.interrupts import main_pending_interrupt

_SUMMARY_SYSTEM_PROMPT = """你是上下文压缩器。把给定对话片段概括为一段中文文本，作为该片段在后续对话中的唯一记忆。

要求:
- 保留关键决策、约束条件、事实信息、未完成事项与下一步计划
- 不要寒暄，不要评价，不要编造原文没有的内容
- 直接输出概括正文，不要 Markdown 标题或代码块"""


def _role_label(message: dict[str, Any]) -> str:
    role = message.get("role", "unknown")
    labels = {
        "human": "用户", "user": "用户",
        "ai": "助手", "assistant": "助手",
        "system": "系统", "tool": "工具",
    }
    label = labels.get(role, role)
    name = message.get("name")
    return f"{label}({name})" if name and role == "tool" else label


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            if isinstance(item, dict)
            else ""
            for item in content
        )
    return str(content)


def _format_transcript(messages: list[dict[str, Any]]) -> str:

    parts: list[str] = []
    for index, message in enumerate(messages, start=1):
        if message.get("curation_synthetic") and not message.get("curation_source_message_ids"):
            continue
        label = _role_label(message)
        content = _message_text(message.get("content", "")).strip()
        if not content:
            continue
        parts.append(f"[{index}] {label}:\n{content}")
    return "\n\n".join(parts)


@model_client_scope()
async def summarize_messages(
    messages: list[dict[str, Any]],
    model_name: str | None,
    app_config: Any,
) -> str:
    transcript = _format_transcript(messages)
    if not transcript.strip():
        raise ValueError("所选范围没有可概括的内容")
    model = create_chat_model(model_name, app_config=app_config)
    from focus.context.requests import frozen_request_messages
    response = await model.ainvoke(frozen_request_messages(_SUMMARY_SYSTEM_PROMPT, transcript))
    text = ""
    content = getattr(response, "content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            if isinstance(item, dict)
            else ""
            for item in content
        )
    if not text.strip():
        raise RuntimeError("摘要模型未返回内容")
    return text.strip()


COMPRESSION_INTERRUPT_TYPE = "compression_request"


async def compression_recovery_payload(
    session: Any, task: Any, checkpointer: Any
) -> dict[str, Any] | None:


    return await main_pending_interrupt(session, task, checkpointer, COMPRESSION_INTERRUPT_TYPE)
