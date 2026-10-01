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
        messages 模式 → **仅助手消息**的 reasoning 事件（独立思考增量）与 tokens 事件（可见正文增量）；
            工具结果、人类消息、系统消息与其它节点输出不产出任何事件——它们只经快照以角色化消息送达，
            且承诺子图（Supervisor）冒泡的消息以其节点名/消息 id 前缀先行排除
        values 模式 → events 事件（data: 完整序列化状态快照）

输入为:
    serialize_message: message — LangChain 消息实例
    chunk_to_events: mode — "messages" | "values"；chunk — astream 产出的原始 chunk；
                     envelope — 基础信封 dict（含 workspace_id/thread_id/agent_id/run_id）
    deserialize_messages: messages — 前端消息 dict 列表（含 tool_calls/tool_call_id/files）

输出为:
    serialize_message → dict；chunk_to_events → list[StreamEvent]（id 留空由 bridge 分配）；
    deserialize_messages → list[BaseMessage]（结构非法时抛 ValueError）

具体工作流为:
    (1) messages 模式: chunk 为 (message_chunk, metadata) 元组 → 排除承诺子图消息与非助手消息
        → stream_reasoning 取独立思考增量组装 reasoning 事件、stream_text 取可见正文组装 tokens 事件
        （node 取 metadata.langgraph_node）
    (2) values 模式: chunk 为完整状态 dict → serialize_value 递归序列化 → events 事件
    (3) 所有事件 data 为 build_envelope 信封，前端按 run_id 独立路由
        UI codec 排除 typed authority、opaque Provider 载荷和运行控制消息；合同展示关系由 history.display 编译，权威恢复使用 focus.history
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
from focus.history.display import display_messages


_COMMITMENT_SUBGRAPH_NODES = frozenset({"prepare_call", "delegate_with_review", "human_review"})


def serialize_message(message: BaseMessage) -> dict[str, Any]:

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
    content = message.content
    if isinstance(message, AIMessage) and isinstance(content, list):
        content = [block for block in content if not isinstance(block, dict)
                   or block.get("type") not in {"reasoning", "compaction"}]
    result: dict[str, Any] = {"role": role, "content": content}
    if message.id:
        result["id"] = message.id
    if isinstance(message, AIMessage) and message.tool_calls:
        result["tool_calls"] = message.tool_calls
        result["locked"] = True
    if isinstance(message, AIMessage):
        reasoning = message.additional_kwargs.get("reasoning_content")
        if reasoning is not None:
            result["reasoning_content"] = reasoning
    if isinstance(message, ToolMessage):
        result["tool_call_id"] = message.tool_call_id
        result["name"] = message.name
        result["status"] = message.status
        result["locked"] = True
    files = message.additional_kwargs.get("files") if message.additional_kwargs else None
    if files:
        result["files"] = files
    compression = message.additional_kwargs.get("compression") if message.additional_kwargs else None
    if compression:
        result["compression"] = _display_compression(compression)
    if message.additional_kwargs and message.additional_kwargs.get("curation_synthetic"):
        result["curation_synthetic"] = True
    return result


def _display_compression(metadata: dict) -> dict:
    from focus.history import deserialize_history_message

    return {**metadata, "source": [serialize_message(deserialize_history_message(record))
                                  for record in metadata.get("source", ())]}


def serialize_value(value: Any) -> Any:

    if isinstance(value, BaseMessage):
        return serialize_message(value)
    if isinstance(value, dict):
        return {key: serialize_value(item) for key, item in value.items()
                if key not in {"execution_items", "world_state_snapshot", "request_manifest", "inbox_delivery"}}
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(item, BaseMessage) for item in value):
            return [serialize_message(item) for item in display_messages(value)]
        return [serialize_value(item) for item in value if not _hidden_context(item)]
    return value


def _hidden_context(value: Any) -> bool:
    if not isinstance(value, BaseMessage):
        return False
    context = value.additional_kwargs.get("focus_context", {})
    return context.get("scope") in {"runtime", "round"} or context.get("kind") == "selected_context"


def stream_text(content: Any) -> str:

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


def stream_reasoning(message: BaseMessage) -> str:

    reasoning = (getattr(message, "additional_kwargs", None) or {}).get("reasoning_content")
    return reasoning if isinstance(reasoning, str) else ""


def validate_messages(messages: list[dict[str, Any]]) -> None:


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
            if message.get("reasoning_content") is not None:
                kwargs.setdefault("additional_kwargs", {})["reasoning_content"] = message["reasoning_content"]
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
                    status=message.get("status", "success"),
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

    return {
        "workspace_id": workspace_id,
        "thread_id": thread_id,
        "agent_id": agent_id,
        "run_id": run_id,
        "event": event,
        "data": data,
    }


def serialize_interrupt(value: Any) -> Any:


    from langgraph.types import Interrupt

    if isinstance(value, Interrupt):
        return {"id": value.id, "value": serialize_interrupt(value.value)}
    if isinstance(value, dict):
        return {key: serialize_interrupt(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_interrupt(item) for item in value]
    return serialize_value(value)


def extract_interrupts(chunk: Any) -> list[dict[str, Any]]:


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


    if mode == "messages":
        try:
            message, metadata = chunk
        except (TypeError, ValueError):
            return []


        node = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
        message_id = getattr(message, "id", None)
        if node in _COMMITMENT_SUBGRAPH_NODES or (
            isinstance(message_id, str) and message_id.startswith("commitment-stage-")
        ):
            return []
        if not isinstance(message, AIMessage):
            return []
        events: list[StreamEvent] = []
        reasoning = stream_reasoning(message)
        if reasoning:
            payload = {"content": reasoning, "message_id": message_id, "node": node}
            events.append(StreamEvent(
                id="", event="reasoning",
                data={**envelope, "event": "reasoning", "data": payload},
            ))
        content = stream_text(message.content)
        if content:
            payload = {"content": content, "message_id": message_id, "node": node}
            events.append(StreamEvent(
                id="", event="tokens",
                data={**envelope, "event": "tokens", "data": payload},
            ))
        return events

    if mode == "values":
        payload = serialize_value(chunk)
        return [StreamEvent(id="", event="events", data={**envelope, "event": "events", "data": payload})]

    if mode == "custom":

        payload = serialize_value(chunk)
        return [StreamEvent(id="", event="events", data={**envelope, "event": "events", "data": payload})]

    return []
