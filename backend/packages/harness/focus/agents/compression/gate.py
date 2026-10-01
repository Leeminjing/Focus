"""本文件对外提供 CompressionGate 中间件与 build_compression_gate 工厂函数。

对外提供:
    CompressionGate — 可装配到 create_agent 的压缩门中间件
    build_compression_gate — 按上下文窗口与阈值比例构造压缩门的同步工厂
    apply_compression_ranges — 把压缩范围编译为新的 messages 状态（阈值压缩与
        快捷压缩共用的应用编译器）

输入为:
    apply_compression_ranges(messages, ranges): messages 为当前 BaseMessage 列表；
        ranges 为规范化范围列表（每条 {source_ids, replacement|restore|delete}）
    build_compression_gate(context_window, threshold_ratio): context_window 为窗口上限
        （None 时恒放行）；threshold_ratio 为阈值比例（默认 0.9）

输出为:
    before_model 钩子返回 None（放行）或消息状态更新；wrap_model_call 钩子返回剥离
    压缩元数据后的模型响应。

具体工作流为:
    (1) before_model 估算 messages 与 run 图片附件组成的最终请求用量；未达阈值、
        本轮已取消或窗口未知时放行
    (2) 达阈值时以 interrupt() 暂停图并发起 compression_request；resume 后：
        cancel → 记录该 run 后本轮放行；apply → 校验后按范围重建 messages
        （压缩范围为块、restore 范围原位展开来源原文，均经 RemoveMessage 全量重建）
        显式重建同时清除旧 typed authority、WorldState 基线与 opaque Provider continuation，下一节点重新锚定
        最终请求准备再按原冻结版本补齐当前 Run 引用；工具协议占位使用共享 error repair 合同
    (3) wrap_model_call 在每次模型调用前剥离 messages 的 compression 元数据，
        来源原文永不进入模型上下文
    (4) 校验压缩范围时保护活动运行的 origin 用户材料消息，并继续豁免必看图片依赖消息；
        身份取自 runtime.context，因此保护自然限于当前 run，运行完成后恢复既有压缩语义

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
from focus.agents.material_inputs import RunMaterialInputs
from focus.messages import estimate_model_request_tokens
from focus.messages.request_budget import estimate_request_budget
from focus.context.scoped import scoped_messages
from focus.agents.image_attachment import select_image_messages, project_image_messages
from focus.runtime.runs.events import (
    validate_messages,
)
from focus.history import deserialize_history_messages, serialize_history_message as serialize_message
from focus.history.repair import repair_tool_messages

_COMPRESSION_KWARG = "compression"


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
            continue
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


def apply_compression_ranges(messages: list[BaseMessage], ranges: list[dict]) -> dict[str, Any]:


    by_id = {message.id: message for message in messages if message.id}
    removed: set[str] = set()
    restore_map: dict[str, list[BaseMessage]] = {}
    block_by_first: dict[str, BaseMessage] = {}
    for message_range in ranges:
        source_ids = message_range["source_ids"]
        removed.update(source_ids)
        if message_range.get("restore"):
            for source_id in source_ids:


                restore_map[source_id] = deserialize_history_messages(_compression_source(by_id[source_id]))
            continue
        if message_range.get("delete"):


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
    from focus.history.bridge import branch_messages

    rebuilt = repair_tool_messages(branch_messages(rebuilt), cause="compression")
    validate_messages([serialize_message(message) for message in rebuilt])

    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *rebuilt],
        "execution_items": None,
        "world_state_snapshot": None,
        "request_manifest": None,
    }


class CompressionGate(AgentMiddleware):
    """主 Agent 模型调用前的 human-in-the-loop 压缩门。"""

    def __init__(self, context_window: int | None, threshold_ratio: float) -> None:
        super().__init__()
        self._context_window = context_window
        self._threshold_ratio = threshold_ratio
        self._instructions = ""
        self._tools = ()
        self._model = None
        self._world_state = None

    def configure_request(self, instructions: str, tool_specs: list[dict], *, model=None, world_state=None) -> None:
        self._instructions = instructions
        self._tools = tuple(tool_specs)
        self._model = model
        self._world_state = world_state

    async def abefore_model(self, state, runtime):
        from types import SimpleNamespace
        from focus.security.context import has_security_context
        from focus.security.live_mode import refreshed_runtime_context

        context = runtime.context
        if has_security_context(context):
            context = await refreshed_runtime_context(context)
        return self.before_model(state, SimpleNamespace(context=context))

    def _request_usage(self, state, context, images):
        messages = state.get("messages", [])
        if self._world_state is not None:
            from focus.security.context import has_security_context
            if has_security_context(context):
                messages = self._world_state.preview_messages(state, context)
        messages = scoped_messages(list(messages), str(context.get("run_id", "")))
        if self._model is not None:
            from langchain_core.messages import SystemMessage
            from focus.models.response_projection import ResponsesRequestProjector
            from focus.messages.request_budget import estimate_responses_budget

            projected = project_image_messages(_strip_compression_kwargs(messages), context)
            payload = ResponsesRequestProjector(self._model.provider_contract).build(
                [SystemMessage(content=self._instructions), *projected], model=self._model.model_name,
                tools=self._tools, text=self._model.text,
            )
            return estimate_responses_budget(payload)
        messages = select_image_messages(messages, context.get("origin_message_id"))
        return (estimate_request_budget(messages, self._instructions, self._tools, request_images=images)
                if self._instructions or self._tools else estimate_model_request_tokens(messages, images))

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
        run_materials = RunMaterialInputs.from_context(getattr(runtime, "context", None))
        run_images = run_materials.images
        context = getattr(runtime, "context", None) or {}
        usage = self._request_usage(state, context, run_images.attached)
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
        ranges, error = validate_apply_decision(
            decision,
            messages,
            run_images.required_ids,
            (run_materials.origin_message_id,) if run_materials.origin_message_id else (),
        )
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
