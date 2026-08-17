"""
本文件对外提供统一消息序列化与事件转换模块。

对外提供:
    serialize_message — LangChain BaseMessage → 前端消息 dict（role/content/tool_calls/locked/files）
    validate_messages — 校验前端消息列表结构合法（tool call 关联完整性）
    deserialize_messages — 前端消息 dict → LangChain BaseMessage 列表（含校验）
    serialize_value — 递归序列化（dict/list/BaseMessage）为 JSON 可序列化值
    stream_text — 从 LangChain message chunk 提取可见文本
    build_envelope — 组装统一事件信封 {workspace_id, thread_id, agent_id, run_id, event, data}
    chunk_to_events — 将 astream 的 (mode, chunk) 转换为 StreamEvent 列表:
        messages 模式 → tokens 事件（data: {content, message_id, node}）
        values 模式 → events 事件（data: 完整序列化状态快照）

输入:
    serialize_message: message — LangChain 消息实例
    chunk_to_events: mode — "messages" | "values"；chunk — astream 产出的原始 chunk；
                     envelope — 基础信封 dict（含 workspace_id/thread_id/agent_id/run_id）
    deserialize_messages: messages — 前端消息 dict 列表（含 tool_calls/tool_call_id/files）

输出:
    serialize_message → dict；chunk_to_events → list[StreamEvent]（id 留空由 bridge 分配）；
    deserialize_messages → list[BaseMessage]（结构非法时抛 ValueError）

具体工作流:
    (1) messages 模式: chunk 为 (message_chunk, metadata) 元组 → stream_text 提取增量文本
        → 组装 tokens 事件（node 取 metadata.langgraph_node）
    (2) values 模式: chunk 为完整状态 dict → serialize_value 递归序列化 → events 事件
    (3) 所有事件 data 为 build_envelope 信封，前端按 run_id 独立路由
    (4) deserialize_messages: validate_messages 校验 tool call 关联完整性 → 按角色还原 BaseMessage

示例:
    envelope = build_envelope("ws-1", "th-1", "main:th-1", "run-1", "tokens", {"content": "你"})
    events = chunk_to_events("messages", (chunk, {"langgraph_node": "model"}), base)
    events = chunk_to_events("values", {"messages": [HumanMessage(content="hi")]}, base)
    messages = deserialize_messages([{"role": "human", "content": "你好"}])
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from focus.runtime.stream_bridge.schemas import StreamEvent

# 承诺子图（Supervisor）节点名：其消息冒泡到父图 messages 通道时以此为特征排除
_COMMITMENT_SUBGRAPH_NODES = frozenset({"prepare_call", "delegate_with_review", "human_review"})


def serialize_message(message: BaseMessage) -> dict[str, Any]:
    """LangChain BaseMessage → 前端消息 dict。"""
    if isinstance(message, HumanMessage):
        role = "human"
    elif isinstance(message, AIMessage):
        role = "ai"
    elif isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, ToolMessage):
        role = "tool"
    else:
        role = message.type
    result: dict[str, Any] = {"role": role, "content": message.content}
    if message.id:
        result["id"] = message.id
    if isinstance(message, AIMessage) and message.tool_calls:
        result["tool_calls"] = message.tool_calls
        result["locked"] = True
    if isinstance(message, ToolMessage):
        result["tool_call_id"] = message.tool_call_id
        result["name"] = message.name
        result["locked"] = True
    files = message.additional_kwargs.get("files") if message.additional_kwargs else None
    if files:
        result["files"] = files
    compression = message.additional_kwargs.get("compression") if message.additional_kwargs else None
    if compression:
        result["compression"] = compression
    if message.additional_kwargs and message.additional_kwargs.get("curation_synthetic"):
        result["curation_synthetic"] = True
    return result


def serialize_value(value: Any) -> Any:
    """递归将 LangChain 值序列化为 JSON 可序列化等价值。"""
    if isinstance(value, BaseMessage):
        return serialize_message(value)
    if isinstance(value, dict):
        return {key: serialize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_value(item) for item in value]
    return value


def stream_text(content: Any) -> str:
    """从 LangChain message chunk 提取可见文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def validate_messages(messages: list[dict[str, Any]]) -> None:
    """校验前端消息列表结构合法：角色有效、tool call 与 ToolMessage 关联完整。

    输入:
        messages: list[dict] — 前端消息列表

    输出:
        None — 结构合法；非法时抛 ValueError（含消息序号）
    """
    pending_calls: dict[str, int] = {}
    resolved_calls: set[str] = set()
    for index, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content", "")
        unresolved = set(pending_calls) - resolved_calls
        if unresolved and role != "tool":
            raise ValueError(
                f"消息 {index + 1} 之前必须紧跟完成工具结果: {', '.join(sorted(unresolved))}"
            )
        if role not in {"human", "user", "ai", "assistant", "system", "tool"}:
            raise ValueError(f"消息 {index + 1} 的角色无效")
        if not isinstance(content, (str, list)):
            raise ValueError(f"消息 {index + 1} 的 content 必须是文本或内容块")
        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            if role not in {"ai", "assistant"}:
                raise ValueError(f"消息 {index + 1} 的 tool_calls 只能属于 AIMessage")
            for call in tool_calls:
                call_id = call.get("id") if isinstance(call, dict) else None
                if not call_id or call_id in pending_calls:
                    raise ValueError(f"消息 {index + 1} 包含无效或重复 tool call id")
                pending_calls[call_id] = index
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not call_id or call_id not in pending_calls or call_id in resolved_calls:
                raise ValueError(f"消息 {index + 1} 的 ToolMessage 没有合法调用方")
            resolved_calls.add(call_id)
    unresolved = set(pending_calls) - resolved_calls
    if unresolved:
        raise ValueError(f"工具调用缺少结果: {', '.join(sorted(unresolved))}")


def deserialize_messages(
    messages: list[dict[str, Any]], validate: bool = True
) -> list[BaseMessage]:
    """前端消息 dict 列表 → LangChain BaseMessage 列表（默认先校验结构）。

    输入:
        messages: list[dict] — 前端消息（role/content/id/files/tool_calls/tool_call_id）
        validate: bool — 是否先做 tool call 关联完整性校验；压缩块来源恢复等
            局部切片可能孤立于调用方，由调用方后续统一修复时传 False

    输出:
        list[BaseMessage] — 按角色还原的消息实例

    工作流:
        (1) validate=True 时 validate_messages 校验 tool call 关联完整性
        (2) human/user → HumanMessage；ai/assistant → AIMessage（含 tool_calls）；
            system → SystemMessage；tool → ToolMessage
    """
    if validate:
        validate_messages(messages)
    result: list[BaseMessage] = []
    for message in messages:
        role = message.get("role")
        kwargs: dict[str, Any] = {}
        if message.get("id"):
            kwargs["id"] = message["id"]
        if message.get("files"):
            kwargs["additional_kwargs"] = {"files": message["files"]}
        if message.get("compression"):
            kwargs.setdefault("additional_kwargs", {})["compression"] = message["compression"]
        if role in {"human", "user"}:
            result.append(HumanMessage(content=message.get("content", ""), **kwargs))
        elif role in {"ai", "assistant"}:
            if message.get("tool_calls"):
                kwargs["tool_calls"] = message["tool_calls"]
            result.append(AIMessage(content=message.get("content", ""), **kwargs))
        elif role == "system":
            result.append(SystemMessage(content=message.get("content", ""), **kwargs))
        else:
            result.append(
                ToolMessage(
                    content=message.get("content", ""),
                    tool_call_id=message["tool_call_id"],
                    name=message.get("name"),
                    **kwargs,
                )
            )
    return result


def build_envelope(
    workspace_id: str | None,
    thread_id: str,
    agent_id: str,
    run_id: str,
    event: str,
    data: Any,
) -> dict[str, Any]:
    """组装统一事件信封。"""
    return {
        "workspace_id": workspace_id,
        "thread_id": thread_id,
        "agent_id": agent_id,
        "run_id": run_id,
        "event": event,
        "data": data,
    }


def serialize_interrupt(value: Any) -> Any:
    """递归序列化 LangGraph Interrupt（id + value）为 JSON 可序列化 dict。

    输入:
        value: Any — Interrupt 实例或包含它的任意结构

    输出:
        Any — {"id": ..., "value": ...} 或递归序列化后的等价结构
    """
    from langgraph.types import Interrupt

    if isinstance(value, Interrupt):
        return {"id": value.id, "value": serialize_interrupt(value.value)}
    if isinstance(value, dict):
        return {key: serialize_interrupt(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_interrupt(item) for item in value]
    return serialize_value(value)


def extract_interrupts(chunk: Any) -> list[dict[str, Any]]:
    """递归提取 chunk 中所有 LangGraph Interrupt 对象。

    输入:
        chunk: Any — astream 产出的 chunk（values 快照可能含 __interrupt__ 键）

    输出:
        list[dict[str, Any]] — 序列化后的 Interrupt 列表；无中断返回空列表
    """
    from langgraph.types import Interrupt

    if isinstance(chunk, Interrupt):
        return [serialize_interrupt(chunk)]
    if isinstance(chunk, dict):
        result: list[dict[str, Any]] = []
        for value in chunk.values():
            result.extend(extract_interrupts(value))
        return result
    if isinstance(chunk, (list, tuple)):
        result = []
        for item in chunk:
            result.extend(extract_interrupts(item))
        return result
    return []


def chunk_to_events(mode: str, chunk: Any, envelope: dict[str, Any]) -> list[StreamEvent]:
    """将 astream 的 (mode, chunk) 转换为统一信封 StreamEvent 列表。

    输入:
        mode: str — "messages"（token 增量）、"values"（完整快照）或 "custom"
        chunk: Any — astream 产出的原始 chunk
        envelope: dict — 基础信封（workspace_id/thread_id/agent_id/run_id，event/data 由本函数填充）

    输出:
        list[StreamEvent] — tokens / events 事件；无法识别的 chunk 返回空列表
    """
    if mode == "messages":
        try:
            message, metadata = chunk
        except (TypeError, ValueError):
            return []
        # 承诺子图（Supervisor）在 middleware 内 astream 时，其 AIMessage/ToolMessage
        # 会冒泡到父图 messages 通道（langgraph_node 为子图节点名）；这些消息已通过
        # custom 通道（commitment_messages）进入轨迹面板，不得重复发布为 tokens
        # 污染 lead 对话流。
        node = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
        message_id = getattr(message, "id", None)
        if node in _COMMITMENT_SUBGRAPH_NODES or (
            isinstance(message_id, str) and message_id.startswith("commitment-stage-")
        ):
            return []
        content = stream_text(message.content)
        if not content:
            return []
        payload = {
            "content": content,
            "message_id": message_id,
            "node": node,
        }
        return [StreamEvent(id="", event="tokens", data={**envelope, "event": "tokens", "data": payload})]

    if mode == "values":
        payload = serialize_value(chunk)
        return [StreamEvent(id="", event="events", data={**envelope, "event": "events", "data": payload})]

    if mode == "custom":
        # 承诺层各角色真实 messages（commitment_messages）→ events 信封
        payload = serialize_value(chunk)
        return [StreamEvent(id="", event="events", data={**envelope, "event": "events", "data": payload})]

    return []
