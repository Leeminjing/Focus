r"""本文件对外提供 definition revision 的非路由 LangGraph checkpoint 写入端口与实现。

输入为尚无 checkpoint 的 shadow `ContextRevisionRef`、协议合法 execution 前缀、可选真实运行后缀、
graph factory 和 checkpointer；输出为精确 checkpoint id 与初始前缀消息 id。具体工作流为强制 shadow
thread/namespace，用 NamespacedCheckpointer 把物理 namespace 映射成根图视角，依次反序列化并写入
策展前缀与后缀，再从已持久化 state 返回执行身份；不读取或覆盖活动 Context，也不把持久化 namespace
误判成子图路径。
示例：`checkpoint_id, ids = await writer.write(shadow_ref, messages)`。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Awaitable, Callable, Protocol

from backend.app.desktop.context_evolution.schemas import ContextRevisionRef
from focus.runtime.checkpointer.namespaced import NamespacedCheckpointer
from focus.runtime.runs.events import deserialize_messages


class ContextCheckpointWriter(Protocol):
    async def write(
        self,
        shadow_ref: ContextRevisionRef,
        execution_messages: tuple[dict[str, Any], ...],
        suffix_messages: tuple[dict[str, Any], ...] = (),
    ) -> tuple[str, tuple[str, ...]]: ...


class LangGraphContextCheckpointWriter:
    def __init__(
        self,
        graph_factory: Callable[[], Awaitable[Any]],
        checkpointer: Any,
    ) -> None:
        self._graph_factory = graph_factory
        self._checkpointer = checkpointer

    async def write(
        self,
        shadow_ref: ContextRevisionRef,
        execution_messages: tuple[dict[str, Any], ...],
        suffix_messages: tuple[dict[str, Any], ...] = (),
    ) -> tuple[str, tuple[str, ...]]:
        self._require_shadow_identity(shadow_ref)
        graph = await self._graph_factory()
        graph.checkpointer = NamespacedCheckpointer(
            self._checkpointer, shadow_ref.checkpoint_ns
        )
        messages = deserialize_messages(deepcopy(list(execution_messages)))
        for raw, message in zip(execution_messages, messages):
            if raw.get("curation_synthetic"):
                message.additional_kwargs["curation_synthetic"] = True
        suffix = deserialize_messages(deepcopy(list(suffix_messages)))
        config = {
            "configurable": {
                "thread_id": shadow_ref.execution_thread_id,
            }
        }
        updated_config = await graph.aupdate_state(
            config, {"messages": [*messages, *suffix]}
        )
        state = await graph.aget_state(updated_config)
        checkpoint_id = state.config.get("configurable", {}).get("checkpoint_id")
        if not checkpoint_id:
            raise RuntimeError("写入 shadow Context checkpoint 后未返回 checkpoint_id")
        initial_ids = tuple(
            message.id
            for message in state.values.get("messages", [])[: len(messages)]
            if message.id
        )
        return checkpoint_id, initial_ids

    @staticmethod
    def _require_shadow_identity(ref: ContextRevisionRef) -> None:
        if not ref.execution_thread_id.startswith("shadow:"):
            raise ValueError("definition candidate 必须使用不可路由 shadow thread")
        if ref.checkpoint_ns != "context-revision-shadow":
            raise ValueError("definition candidate 必须使用 shadow checkpoint namespace")
        if ref.checkpoint_id is not None:
            raise ValueError("写入前 shadow revision 不得已有 checkpoint_id")
