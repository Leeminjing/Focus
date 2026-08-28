"""
本文件对外提供 MemoryService，实现全局记忆库的持久化、来源解析、压缩总结与注入块构建。

对外提供:
    MemoryService — 记忆库业务服务（list/get/create/update/delete + resolve_selection
        + summarize_memory_transcript + build_memory_block）

输入:
    session_factory — 桌面 DB 异步会话工厂
    app_config — 组合根配置（取默认模型做压缩总结）
    context_reader — Callable，给定 context_id（根/派生 context 的 task_id）返回其
        serialized messages 列表；由 DesktopService 注入（避免循环依赖）

输出:
    list[dict] / dict — 记忆库列表与单条 payload
    str — 来源解析后的 transcript 或 `<memory>` 注入块

具体工作流:
    (1) CRUD：list/get/create/update/delete 直接读写 memory_items 表
    (2) resolve_selection：按 source.type（session/messages/text/manual）经 context_reader
        取上下文消息，按 message_ids（非连续）或 ranges（文字区间）切片，拼成 transcript
    (3) summarize_memory_transcript：复用 create_chat_model + 单次 ainvoke 生成中文摘要
    (4) build_memory_block：把选中记忆包装为 `<memory>...</memory>` 块供 system prompt 注入

示例:
    service = MemoryService(session_factory, app_config, context_reader)
    await service.create(MemoryCreate(title="...", content="...", source=...))
    block = await service.build_memory_block(["mem-1", "mem-2"])
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from focus.models import create_chat_model
from langchain_core.messages import HumanMessage, SystemMessage

from backend.app.desktop.models import Memory

logger = logging.getLogger(__name__)

_MAIN_RUNTIME_MEMORY_KEY = "_main_run_memory_ids"

# === 记忆压缩提示词（两种语义，精心设计） ===
#
# 设计原则（依据记忆压缩最佳实践）：
#   1. 输出「持久事实」而非对话过程复述 —— 决策、约束、事实、未决事项、下一步。
#   2. 去噪：丢弃寒暄、过程性日志、可由上下文重新推导的中间状态。
#   3. 可溯源：分段记忆每段独立可读，并通过 source_ref 关联到具体来源。
#   4. 格式约束：直接输出正文，不写标题/列表/代码块，避免污染注入后的系统提示词。
#   5. 限长：宁可精炼，避免一大段无重点的长文。
#
# 两套提示词的差异只在「组织方式」：
#   - COMPLETE：把多来源当作一个整体，跨来源去重、整合成一份全局纵览。
#   - SEGMENT：把每个来源当独立记忆段提炼，段与段不合并，保留各自重点。

# 「哪些信息值得留下」的共享准则。两套提示词都引用它，避免重复。
_MEMORY_FACT_RULES = """\
应该留下的信息（按价值优先级）：
- 关键决策及背后的原因
- 硬性约束、规则、边界条件（例如技术栈限制、必须遵守的约定）
- 不可从代码/日志直接看出的背景与上下文
- 未完成的事项、未解决的问题、明确的下一步计划
- 用户明确表达的需求、偏好、验收标准

不要留下的信息：
- 寒暄、客套、语气词
- 过程性描述（"我先尝试了 A，又试了 B"）
- 可由当前文件/上下文直接重新推导的临时中间状态
- 无信息量的重复与评述"""

_MEMORY_COMPLETE_PROMPT = f"""\
你是记忆压缩器。给定来自多个会话/消息/文字片段的原始对话，把它们整合成一份「完整记忆」。

这份记忆将作为新会话的长期背景注入。请把多个来源当作一个整体来读，跨来源去重、合并同类信息，提炼出一条贯穿的上下文脉络，而不是逐来源罗列。

{_MEMORY_FACT_RULES}

输出要求：
- 只用一段连贯的中文正文，逻辑上先讲背景/总体任务，再讲关键约束与决策，最后讲未完成事项与下一步
- 直接输出正文，不要标题、序号、项目符号、代码块、Markdown
- 不要寒暄、不要评价、不要编造原文没有的内容
- 控制在 250 字以内，聚焦最重要的信息"""

_MEMORY_SEGMENT_PROMPT = f"""\
你是记忆压缩器。给定某一个会话/消息片段/文字片段的原始对话，把它提炼成一段独立、自包含的「记忆段」。

每个来源都会被压缩成**独立的一段**，段与段之间不合并。因此这一段需要自成一体：脱离其他来源后仍然完整可读，清楚交代"这个来源讲了什么、有什么值得记住的"。

{_MEMORY_FACT_RULES}

输出要求：
- 只用一段简短的中文正文，直接提炼该来源的最重要信息
- 直接输出正文，不要标题、序号、项目符号、代码块、Markdown
- 不要寒暄、不要评价、不要编造原文没有的内容
- 控制在 120 字以内，聚焦这一来源独有的重点"""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemorySource(_Strict):
    type: str  # session | messages | text | manual
    context_id: str | None = None
    message_ids: list[str] | None = None
    parts: list[dict[str, Any]] | None = None  # text 来源: [{message_id, ranges:[{start,end}]}]
    text: str | None = None  # manual 来源


class MemorySelection(_Strict):
    sources: list[MemorySource] = []


class MemoryCreate(_Strict):
    title: str
    content: str
    content_mode: str = "complete"  # segmented | complete
    segments: list[dict[str, Any]] = Field(default_factory=list)
    source_kind: str = "manual"
    source: MemorySelection | None = None


class MemoryUpdate(_Strict):
    title: str | None = None
    content: str | None = None
    content_mode: str | None = None
    segments: list[dict[str, Any]] | None = None


class MemorySummarizeRequest(_Strict):
    selection: MemorySelection
    mode: str = "complete"  # segmented | complete
    model_name: str | None = None


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex


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


class MemoryService:
    def __init__(
        self,
        session_factory: Any,
        app_config: Any,
        context_reader: Callable[[str], Any],
    ) -> None:
        self._session_factory = session_factory
        self._app_config = app_config
        self._context_reader = context_reader

    # === CRUD ===

    async def list(self) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(select(Memory).order_by(Memory.created_at))
            ).all()
        return [self._payload(row) for row in rows]

    async def get(self, memory_id: str) -> dict[str, Any]:
        async with self._session_factory() as session:
            row = await session.get(Memory, memory_id)
        if not row:
            raise HTTPException(404, "记忆不存在")
        return self._payload(row)

    async def create(self, body: MemoryCreate) -> dict[str, Any]:
        async with self._session_factory() as session:
            session.expire_on_commit = False
            row = Memory(
                memory_id=_new_id(),
                title=body.title,
                content=body.content,
                content_mode=body.content_mode,
                segments=body.segments,
                source_kind=body.source_kind,
                source_snapshot=(
                    [source.model_dump() for source in body.source.sources]
                    if body.source
                    else []
                ),
            )
            session.add(row)
            await session.commit()
            return self._payload(row)

    async def update(self, memory_id: str, body: MemoryUpdate) -> dict[str, Any]:
        async with self._session_factory() as session:
            session.expire_on_commit = False
            row = await session.get(Memory, memory_id)
            if not row:
                raise HTTPException(404, "记忆不存在")
            if body.title is not None:
                row.title = body.title
            if body.content is not None:
                row.content = body.content
            if body.content_mode is not None:
                row.content_mode = body.content_mode
            if body.segments is not None:
                row.segments = body.segments
            await session.commit()
            fresh = await session.get(Memory, memory_id, populate_existing=True)
            return self._payload(fresh)

    async def delete(self, memory_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(Memory, memory_id)
            if not row:
                raise HTTPException(404, "记忆不存在")
            await session.delete(row)
            await session.commit()

    # === 来源解析 / 压缩总结 ===

    async def resolve_selection(self, selection: MemorySelection) -> str:
        """把来源选择解析为 transcript 文本；单个来源失效时抛 4xx。"""
        parts: list[str] = []
        for source in selection.sources:
            transcript = await self._resolve_source(source)
            if transcript:
                parts.append(transcript)
        return "\n\n".join(parts)

    async def _resolve_source(self, source: MemorySource) -> str:
        if source.type == "manual":
            return (source.text or "").strip()
        context_id = source.context_id
        if not context_id:
            raise HTTPException(422, "该来源缺少 context_id")
        messages = await self._context_reader(context_id)
        if source.type == "session":
            return self._format_messages(messages)
        if source.type == "messages":
            keep = set(source.message_ids or [])
            selected = [m for m in messages if m.get("id") in keep]
            return self._format_messages(selected)
        if source.type == "text":
            return self._slice_text(messages, source.parts or [])
        raise HTTPException(422, f"未知记忆来源类型: {source.type}")

    def _slice_text(self, messages: list[dict[str, Any]], parts: list[dict[str, Any]]) -> str:
        by_id = {m.get("id"): m for m in messages}
        slices: list[str] = []
        for part in parts:
            message = by_id.get(part.get("message_id"))
            if not message:
                continue
            full = _message_text(message.get("content", ""))
            for span in part.get("ranges", []):
                start = int(span.get("start", 0))
                end = int(span.get("end", len(full)))
                start = max(0, min(start, len(full)))
                end = max(start, min(end, len(full)))
                slices.append(full[start:end])
        return "\n\n".join(slice_ for slice_ in slices if slice_.strip())

    @staticmethod
    def _format_messages(messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for index, message in enumerate(messages, start=1):
            content = _message_text(message.get("content", "")).strip()
            if not content:
                continue
            lines.append(f"[{index}] {message.get('role', 'unknown')}:\n{content}")
        return "\n\n".join(lines)

    async def summarize(self, body: MemorySummarizeRequest) -> dict[str, Any]:
        """按压缩语义生成记忆草稿。

        - mode="complete"：把全部来源合并为一整份压缩，返回 {mode, content}。
        - mode="segmented"：每个来源各自压缩成一段，返回 {mode, segments, content}
          （content 为各段正文的分隔拼接，供展示/注入）。

        任一来源为空或无可用内容时抛 ValueError。
        """
        if body.mode == "segmented":
            return await self._summarize_segmented(body)
        return await self._summarize_complete(body)

    async def _summarize_complete(self, body: MemorySummarizeRequest) -> dict[str, Any]:
        transcript = await self.resolve_selection(body.selection)
        if not transcript.strip():
            raise ValueError("所选来源没有可概括的内容")
        model = create_chat_model(body.model_name, app_config=self._app_config)
        response = await model.ainvoke(
            [SystemMessage(content=_MEMORY_COMPLETE_PROMPT), HumanMessage(content=transcript)]
        )
        content = _message_text(getattr(response, "content", "")).strip()
        if not content:
            # 可观测性：模型返回空时给出关键线索（不含原文/密钥），避免笼统报错。
            raise RuntimeError(
                "摘要模型未返回内容: "
                f"response_type={type(response).__name__}, "
                f"model={getattr(model, 'model_name', None)}, "
                f"api_base={getattr(model, 'openai_api_base', None)}, "
                f"api_key_len={len(getattr(model, 'openai_api_key', '') or '')}"
            )
        return {"mode": "complete", "content": content}

    async def _summarize_segmented(self, body: MemorySummarizeRequest) -> dict[str, Any]:
        sources = body.selection.sources or []
        if not sources:
            raise ValueError("未选择任何记忆来源")
        model = create_chat_model(body.model_name, app_config=self._app_config)
        segments: list[dict[str, Any]] = []
        # 逐来源解析并压缩；单个来源失效时记为该来源的占位段，不中断其余来源。
        for index, source in enumerate(sources, start=1):
            transcript = (await self._resolve_source(source)).strip()
            if not transcript:
                segments.append({"title": f"来源 {index}", "body": "[本来源无可概括内容]", "source_ref": self._source_ref(source)})
                continue
            response = await model.ainvoke(
                [SystemMessage(content=_MEMORY_SEGMENT_PROMPT), HumanMessage(content=transcript)]
            )
            body_text = _message_text(getattr(response, "content", "")).strip()
            if not body_text:
                body_text = "[本来源压缩失败]"
            segments.append({
                "title": f"来源 {index}",
                "body": body_text,
                "source_ref": self._source_ref(source),
            })
        content = "\n\n".join(segment["body"] for segment in segments)
        if not content.strip():
            raise ValueError("所选来源没有可概括的内容")
        return {"mode": "segmented", "segments": segments, "content": content}

    @staticmethod
    def _source_ref(source: MemorySource) -> dict[str, Any]:
        """给来源一个可读的引用（用于前端段标题与溯源）。"""
        if source.type == "session":
            return {"type": "session", "context_id": source.context_id}
        if source.type == "messages":
            return {"type": "messages", "context_id": source.context_id, "count": len(source.message_ids or [])}
        if source.type == "text":
            return {"type": "text", "context_id": source.context_id, "message_id": source.parts[0]["message_id"] if source.parts else None}
        return {"type": "manual"}

    # === 注入块 ===

    async def build_memory_block(self, memory_ids: list[str]) -> str:
        if not memory_ids:
            return ""
        async with self._session_factory() as session:
            rows = (
                await session.scalars(select(Memory).where(Memory.memory_id.in_(memory_ids)))
            ).all()
        if not rows:
            return ""
        inner = "\n".join(self._render_memory(row) for row in rows)
        return f"<memory>\n{inner}\n</memory>"

    @staticmethod
    def _render_memory(row: Memory) -> str:
        """按压缩语义渲染单条记忆块。

        - complete：`<memory id title>content</memory>`
        - segmented：`<memory id title mode="segmented">` 内多层 `<segment title>` 段。
        """
        if row.content_mode == "segmented" and row.segments:
            segments = "\n".join(
                f"<segment title=\"{MemoryService._safe_attr(seg.get('title', '记忆段'))}\">\n{seg.get('body', '')}\n</segment>"
                for seg in row.segments
            )
            return (
                f"<memory id=\"{row.memory_id}\" title=\"{MemoryService._safe_attr(row.title)}\" mode=\"segmented\">\n"
                f"{segments}\n"
                f"</memory>"
            )
        return f"<memory id=\"{row.memory_id}\" title=\"{MemoryService._safe_attr(row.title)}\">\n{row.content}\n</memory>"

    @staticmethod
    def _safe_attr(value: str) -> str:
        # 洗掉可能破坏 XML 属性的字符，避免注入后污染系统提示词。
        return str(value).replace('"', "&#34;").replace("<", "&lt;").replace(">", "&gt;")

    @staticmethod
    def _payload(row: Memory) -> dict[str, Any]:
        return {
            "memory_id": row.memory_id,
            "title": row.title,
            "content": row.content,
            "content_mode": row.content_mode,
            "segments": row.segments,
            "source_kind": row.source_kind,
            "source_snapshot": row.source_snapshot,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
