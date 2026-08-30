"""本文件对外提供桌面压缩流程的摘要生成与恢复投影函数。

对外提供:
    summarize_messages — 用指定模型为选定消息范围生成中文候选摘要
    compression_recovery_payload — 从主图 checkpoint 投影待确认压缩请求的恢复状态

输入:
    summarize_messages: messages — 面板消息快照（serialize_message 产物）；
        model_name — 目标模型名，None 时取默认；app_config — 组合根配置；
        forbid_terms — 明文禁止出现在摘要中的词列表（快捷压缩禁提关键词用），
            非空时摘要指令追加禁提条款（用指代表达，模型不写出该词）
    SummarizeRequest: messages / model_name / forbid_terms
    compression_recovery_payload: session — 桌面 DB 会话；task — DesktopThread；
        checkpointer — LangGraph checkpointer

输出:
    summarize_messages → str 候选摘要；compression_recovery_payload → dict | None
    （None 表示无待确认压缩请求；否则为 {"status": processing|resumable|orphaned,
    "request": 原始 compression_request 载荷}）

具体工作流:
    (1) 摘要：消息按 role 分节格式化（跳过 curation_synthetic 占位、剥离 compression
        元数据），以中文概括 system prompt 单次 ainvoke
    (2) 恢复投影：读主图 checkpoint（checkpoint_ns=""）pending_writes 的 __interrupt__
        channel，取最新 type=compression_request 的 Interrupt.value；再按最近一次 main
        run 状态判定 processing/resumable/orphaned

示例:
    summary = await summarize_messages(messages, None, app_config)
    recovery = await compression_recovery_payload(session, task, checkpointer)
"""

import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from focus.models import create_chat_model

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM_PROMPT = """你是上下文压缩器。把给定对话片段概括为一段中文文本，作为该片段在后续对话中的唯一记忆。

要求:
- 保留关键决策、约束条件、事实信息、未完成事项与下一步计划
- 不要寒暄，不要评价，不要编造原文没有的内容
- 直接输出概括正文，不要 Markdown 标题或代码块"""


def _build_summary_prompt(forbid_terms: tuple[str, ...] | list[str] | None = None) -> str:
    """按禁提词列表构造摘要系统指令；无禁提词时返回默认指令。

    输入: forbid_terms — 明文禁止出现的词序列（可空）

    输出: str — 摘要系统提示词；非空时追加不得提及该词的条款（可用指代表达）
    """
    terms = tuple(dict.fromkeys(forbid_terms or ()))
    if not terms:
        return _SUMMARY_SYSTEM_PROMPT
    quoted = "、".join(f"「{term}」" for term in terms)
    return (
        f"{_SUMMARY_SYSTEM_PROMPT}\n"
        f"- 明确禁止出现以下词：{quoted}；如需表达该含义请用指代（如『该方向』），"
        "不得写出这些词"
    )


class SummarizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]]
    model_name: str | None = None
    forbid_terms: list[str] = Field(default_factory=list)


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
    """按 role 分节格式化；跳过 curation_synthetic 占位与 compression 元数据。"""
    parts: list[str] = []
    for index, message in enumerate(messages, start=1):
        if message.get("curation_synthetic"):
            continue
        label = _role_label(message)
        content = _message_text(message.get("content", "")).strip()
        if not content:
            continue
        parts.append(f"[{index}] {label}:\n{content}")
    return "\n\n".join(parts)


def _scrub_terms(text: str, terms: tuple[str, ...] | list[str] | None) -> str:
    """机械剥离摘要中禁止出现的词（最终保证，不依赖 LLM 自觉）。

    输入:
        text: str — 模型产出的摘要正文
        terms: 序列 — 明文禁止出现的词（可为空）

    输出:
        str — 逐词去除后的文本；若去除后为空则回退为通用占位文本

    具体工作流:
        (1) 对每个禁用词做全量字符串替换去除
        (2) 若结果为空（摘要仅由该词构成），回退为 '（该话题已从上下文移除）'
    """
    scrubbed = text
    for term in dict.fromkeys(terms or ()):
        if term:
            scrubbed = scrubbed.replace(term, "")
    if not scrubbed.strip():
        return "（该话题已从上下文移除）"
    return scrubbed.strip()


async def summarize_messages(
    messages: list[dict[str, Any]],
    model_name: str | None,
    app_config: Any,
    forbid_terms: tuple[str, ...] | list[str] | None = None,
) -> str:
    transcript = _scrub_terms(_format_transcript(messages), forbid_terms)
    if not transcript.strip():
        raise ValueError("所选范围没有可概括的内容")
    model = create_chat_model(model_name, app_config=app_config)
    response = await model.ainvoke(
        [
            SystemMessage(content=_build_summary_prompt(forbid_terms)),
            HumanMessage(content=transcript),
        ]
    )
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
    return _scrub_terms(text.strip(), forbid_terms)


def _checkpoint_compression_request(checkpoint: Any) -> dict[str, Any] | None:
    """从 checkpoint pending_writes 读取最新 compression_request interrupt 载荷。"""
    pending_writes = list(getattr(checkpoint, "pending_writes", None) or [])
    for _task_id, channel, raw_value in reversed(pending_writes):
        if channel != "__interrupt__":
            continue
        values = raw_value if isinstance(raw_value, (list, tuple)) else [raw_value]
        for item in reversed(values):
            payload = getattr(item, "value", item)
            if isinstance(payload, dict) and payload.get("type") == "compression_request":
                return dict(payload)
    return None


async def compression_recovery_payload(
    session: Any, task: Any, checkpointer: Any
) -> dict[str, Any] | None:
    config = {"configurable": {"thread_id": task.thread_id, "checkpoint_ns": ""}}
    try:
        checkpoint = await checkpointer.aget_tuple(config)
    except Exception:
        logger.warning(
            "读取主图 checkpoint 失败: thread_id=%s", task.thread_id, exc_info=True
        )
        return None
    if checkpoint is None:
        return None
    request = _checkpoint_compression_request(checkpoint)
    if request is None:
        return None
    from backend.app.desktop.models import DesktopRun
    from sqlalchemy import select

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
