"""本文件对外提供 CompressionGate 中间件与 build_compression_gate 工厂函数。

对外提供:
    CompressionGate — 可装配到 create_agent 的压缩门中间件
    build_compression_gate — 按上下文窗口与阈值比例构造压缩门的同步工厂
    apply_compression_ranges — 把压缩范围编译为新的 messages 状态（阈值压缩与
        快捷压缩共用的应用编译器）

输入:
    apply_compression_ranges(messages, ranges): messages 为当前 BaseMessage 列表；
        ranges 为规范化范围列表（每条 {source_ids, replacement|restore|delete}）
    build_compression_gate(context_window, threshold_ratio): context_window 为窗口上限
        （None 时恒放行）；threshold_ratio 为阈值比例（默认 0.9）

输出:
    before_model 钩子返回 None（放行）或消息状态更新；wrap_model_call 钩子返回剥离
    压缩元数据后的模型响应。

具体工作流:
    (1) before_model 估算当前 messages 用量；未达阈值、本轮已取消或窗口未知时放行
    (2) 达阈值时以 interrupt() 暂停图并发起 compression_request；resume 后：
        cancel → 记录该 run 后本轮放行；apply → 校验后按范围重建 messages
        （压缩范围为块、restore 范围原位展开来源原文，均经 RemoveMessage 全量重建）
    (3) wrap_model_call 在每次模型调用前剥离 messages 的 compression 元数据，
        来源原文永不进入模型上下文

示例:
    middlewares = [build_compression_gate(context_window=131072, threshold_ratio=0.9)]
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.messages import RemoveMessage
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.config import get_config
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import interrupt

from focus.agents.compression.schemas import validate_apply_decision
from focus.agents.compression.tokens import estimate_messages_tokens
from focus.runtime.runs.events import (
    deserialize_messages,
    serialize_message,
    validate_messages,
)

_COMPRESSION_KWARG = "compression"

# ponytail: 进程内取消过的 run 集合（每取消一次仅增一条 run id，桌面进程生命周期内可忽略）。
# 不依赖 runtime.context 字典可变性：空 dict 会被 LangChain Runtime.merge 以 falsy 语义替换，
# 直接改写 context 的标记在跨模型调用时不可靠；execution_info.run_id 恒为 None，
# configurable.run_id 由 worker 每轮 run 显式写入（resume 为新 run 自然重置）。
_cancelled_runs: set[str] = set()


def _run_id() -> str:
    try:
        configurable = get_config().get("configurable", {})
    except RuntimeError:
        configurable = {}
    return str(configurable.get("run_id") or "")


def _strip_compression_kwargs(messages: list[BaseMessage]) -> list[BaseMessage]:
    cleaned: list[BaseMessage] = []
    changed = False
    for message in messages:
        kwargs = getattr(message, "additional_kwargs", None)
        metadata = (
            kwargs.get(_COMPRESSION_KWARG)
            if isinstance(kwargs, dict)
            else None
        )
        if isinstance(metadata, dict) and metadata.get("deleted"):
            changed = True
            continue  # 删除墓碑：不进模型，仅随 checkpoint 保留来源供前端展示/恢复
        if isinstance(kwargs, dict) and _COMPRESSION_KWARG in kwargs:
            changed = True
            kwargs = {
                key: value
                for key, value in kwargs.items()
                if key != _COMPRESSION_KWARG
            }
            message = message.model_copy(update={"additional_kwargs": kwargs})
        cleaned.append(message)
    return cleaned if changed else messages


def _compression_source(message: BaseMessage) -> list[dict]:
    metadata = getattr(message, "additional_kwargs", {}).get(_COMPRESSION_KWARG, {})
    return metadata.get("source", [])


_SYNTHETIC_TOOL_RESULT = "[Focus placeholder: tool result omitted]"


def _repair_protocol(messages: list[BaseMessage]) -> list[BaseMessage]:
    """修复用户自由划范围拆散 tool-call 组产生的悬空协议（f15 执行投影同款语义）。

    悬空 ToolMessage（tool_call_id 无调用方）→ 文本化降级为 human 消息（保留原 id，
    focus-degraded 包装）；AI tool_calls 缺结果 → 补合成占位 ToolMessage
    （curation_synthetic 标记、稳定合成 id）。未涉及的消息原样保留。
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    pending: dict[str, dict] = {}
    resolved: set[str] = set()
    repaired: list[BaseMessage] = []

    def finish_pending() -> None:
        for call_id, call in list(pending.items()):
            if call_id in resolved:
                continue
            repaired.append(
                ToolMessage(
                    content=_SYNTHETIC_TOOL_RESULT,
                    tool_call_id=call_id,
                    name=call.get("name") or "tool",
                    id=f"focus-synthetic-tool-result-{call_id[:16]}",
                    additional_kwargs={"curation_synthetic": True},
                )
            )
            resolved.add(call_id)

    for message in messages:
        if isinstance(message, ToolMessage):
            call_id = message.tool_call_id
            if call_id in pending and call_id not in resolved:
                resolved.add(call_id)
                repaired.append(message)
                continue
            repaired.append(
                HumanMessage(
                    content=(
                        f'<focus-degraded-message role="tool" name="{message.name or ""}">\n'
                        f"{message.content}\n</focus-degraded-message>"
                    ),
                    id=message.id,
                )
            )
            continue
        if pending and set(pending) - resolved:
            finish_pending()
            pending = {}
            resolved = set()
        if isinstance(message, AIMessage) and message.tool_calls:
            pending = {call["id"]: call for call in message.tool_calls}
            resolved = set()
        repaired.append(message)
    if pending and set(pending) - resolved:
        finish_pending()
    return repaired


def apply_compression_ranges(messages: list[BaseMessage], ranges: list[dict]) -> dict[str, Any]:
    """把压缩范围编译为新的 messages 状态（阈值压缩与快捷压缩共用）。

    输入:
        messages: list[BaseMessage] — 当前 graph state 的完整 messages
        ranges: list[dict] — 规范化范围列表，每条为
            {source_ids, replacement}（压缩块）或 {source_ids, restore}（展开来源）
            或 {source_ids, delete}（删除墓碑）；来源随块元数据持久化

    输出:
        dict — {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *rebuilt]} 状态更新

    具体工作流:
        (1) 逐范围收集 removed / restore_map / block_by_first（块与墓碑均携带来源元数据）
        (2) 按原消息顺序重建：restore 展开来源、块/墓碑原位替换、其余保留
        (3) 经 _repair_protocol 修复拆散 tool-call 组的悬空协议
        (4) validate_messages 校验重建后协议合法后返回
    """
    by_id = {message.id: message for message in messages if message.id}
    removed: set[str] = set()
    restore_map: dict[str, list[BaseMessage]] = {}
    block_by_first: dict[str, BaseMessage] = {}
    for message_range in ranges:
        source_ids = message_range["source_ids"]
        removed.update(source_ids)
        if message_range.get("restore"):
            for source_id in source_ids:
                # 来源切片可能孤立于调用方（用户自由划范围），跳过局部校验，
                # 由重建后的 _repair_protocol + validate_messages 统一兜底
                restore_map[source_id] = deserialize_messages(
                    _compression_source(by_id[source_id]), validate=False
                )
            continue
        if message_range.get("delete"):
            # 删除墓碑：content 为空、携带来源与 deleted 标记；模型不可见（wrap_model_call 过滤），
            # 前端据此保留原会话展示并支持"恢复原消息"撤销删除
            block_id = uuid.uuid4().hex
            block_by_first[source_ids[0]] = HumanMessage(
                content="",
                additional_kwargs={
                    _COMPRESSION_KWARG: {
                        "block_id": block_id,
                        "source": [
                            serialize_message(by_id[source_id])
                            for source_id in source_ids
                        ],
                        "deleted": True,
                        "compressed_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
                id=block_id,
            )
            continue
        block_id = uuid.uuid4().hex
        block = HumanMessage(
            content=message_range["replacement"],
            additional_kwargs={
                _COMPRESSION_KWARG: {
                    "block_id": block_id,
                    "source": [
                        serialize_message(by_id[source_id])
                        for source_id in source_ids
                    ],
                    "compressed_at": datetime.now(timezone.utc).isoformat(),
                }
            },
            id=block_id,
        )
        block_by_first[source_ids[0]] = block
    rebuilt: list[BaseMessage] = []
    for message in messages:
        if message.id in restore_map:
            rebuilt.extend(restore_map[message.id])
        elif message.id in block_by_first:
            rebuilt.append(block_by_first[message.id])
        elif message.id in removed:
            continue
        else:
            rebuilt.append(message)
    rebuilt = _repair_protocol(rebuilt)
    validate_messages([serialize_message(message) for message in rebuilt])
    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *rebuilt]}


class CompressionGate(AgentMiddleware):
    """主 Agent 模型调用前的 human-in-the-loop 压缩门。"""

    def __init__(self, context_window: int | None, threshold_ratio: float) -> None:
        super().__init__()
        self._context_window = context_window
        self._threshold_ratio = threshold_ratio

    @property
    def _limit(self) -> int | None:
        if not self._context_window:
            return None
        return int(self._context_window * self._threshold_ratio)

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        limit = self._limit
        if limit is None:
            return None
        run_id = _run_id()
        if run_id in _cancelled_runs:
            return None
        messages = list(state.get("messages", []))
        usage = estimate_messages_tokens(messages)
        if usage < limit:
            return None
        decision = interrupt(
            {
                "type": "compression_request",
                "usage": usage,
                "limit": self._context_window,
                "ratio": self._threshold_ratio,
            }
        )
        if not isinstance(decision, dict) or decision.get("type") != "compression":
            return None
        if decision.get("decision") == "cancel":
            if run_id:
                _cancelled_runs.add(run_id)
            return None
        if decision.get("decision") != "apply":
            return None
        ranges, error = validate_apply_decision(decision, messages)
        if error:
            raise ValueError(f"压缩 apply 载荷非法: {error}")
        return apply_compression_ranges(messages, ranges)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(request.override(messages=_strip_compression_kwargs(request.messages)))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(request.override(messages=_strip_compression_kwargs(request.messages)))


def build_compression_gate(
    context_window: int | None, threshold_ratio: float = 0.9
) -> CompressionGate:
    return CompressionGate(context_window=context_window, threshold_ratio=threshold_ratio)
