r"""本文件对外提供 ContextRecoveryPlanCompiler，用一个权威 revision 编译证据保持且协议合法的恢复 Lane plan。

输入为精确 source revision、不可变 authored 消息及触发恢复的 Run identity 与原因；输出为仅含 CopyMessage、完整
ToolExchange 和带来源的 unresolved ComposeMessage 的 CreateLanePlan，或无法安全保留证据时的 ValueError。具体工作流为逐组
解析消息，完整交换原样重建，只隔离无结果的终端交换；非终端、部分结果或孤立 ToolMessage 均拒绝自动恢复。示例：
`plan = ContextRecoveryPlanCompiler().compile(source, messages, run_id="r1", reason="cancelled")`。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.app.desktop.agent_loop.context_recovery.contracts import CONTEXT_RECOVERY_COMPILER_VERSION
from backend.app.desktop.context_curation.contracts import (
    ComposeMessage,
    CopyMessage,
    CreateLanePlan,
    NamespacedMessageRef,
    ToolExchange,
    ToolExchangeCall,
)
from backend.app.desktop.context_evolution import ContextRevisionRef


class ContextRecoveryPlanCompiler:
    def compile(
        self,
        source: ContextRevisionRef,
        messages: tuple[dict[str, Any], ...],
        *,
        run_id: str,
        reason: str,
    ) -> CreateLanePlan:
        items = self._items(source, messages, run_id=run_id, reason=reason)
        if not items:
            raise ValueError("恢复来源没有可安全保留的消息证据")
        fingerprint = self._hash(
            {
                "compiler_version": CONTEXT_RECOVERY_COMPILER_VERSION,
                "source": source.model_dump(mode="json"),
                "run_id": run_id,
                "items": [item.model_dump(mode="json") for item in items],
            }
        )
        return CreateLanePlan(
            action="create",
            purpose=f"恢复 Context {source.context_id} 的可运行执行历史",
            source_frontier=(source,),
            items=tuple(items),
            lane_policy={
                "recovery_compiler_version": CONTEXT_RECOVERY_COMPILER_VERSION,
                "source_run_id": run_id,
                "semantic_fingerprint": fingerprint,
                "workspace_mode": "read_only",
                "recovery_protocol_policy": "exclude_terminal_unresolved_exchange_v2",
            },
        )

    def _items(
        self,
        source: ContextRevisionRef,
        messages: tuple[dict[str, Any], ...],
        *,
        run_id: str,
        reason: str,
    ) -> list[CopyMessage | ComposeMessage | ToolExchange]:
        items: list[CopyMessage | ComposeMessage | ToolExchange] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            ref = self._ref(source, message)
            role = message.get("role")
            calls = tuple(message.get("tool_calls") or ())
            if role == "tool":
                raise ValueError("孤立 ToolMessage 无法安全自动恢复")
            if role in {"ai", "assistant"} and calls:
                results, next_index = self._results(messages, index + 1, calls)
                if len(results) == len(calls):
                    items.append(self._exchange(source, message, results))
                    index = next_index
                    continue
                if next_index != len(messages):
                    raise ValueError("非终端未闭合工具交换无法安全自动恢复")
                if results:
                    raise ValueError("部分工具结果不能在自动恢复中丢弃")
                items.append(
                    ComposeMessage(
                        type="compose_message",
                        role="ai",
                        content=self._interruption_content(message, run_id, reason),
                        sources=(ref,),
                    )
                )
                index = next_index
                continue
            items.append(CopyMessage(type="copy_message", source=ref))
            index += 1
        return items

    @staticmethod
    def _results(
        messages: tuple[dict[str, Any], ...],
        start: int,
        calls: tuple[dict[str, Any], ...],
    ) -> tuple[tuple[dict[str, Any], ...], int]:
        expected = {str(call.get("id")) for call in calls}
        results: list[dict[str, Any]] = []
        index = start
        while index < len(messages) and messages[index].get("role") == "tool":
            candidate = messages[index]
            call_id = str(candidate.get("tool_call_id") or "")
            if call_id not in expected or any(str(item.get("tool_call_id")) == call_id for item in results):
                raise ValueError("ToolMessage 与来源 tool call 不匹配")
            results.append(candidate)
            index += 1
        return tuple(results), index

    def _exchange(
        self,
        source: ContextRevisionRef,
        caller: dict[str, Any],
        results: tuple[dict[str, Any], ...],
    ) -> ToolExchange:
        by_id = {str(item["tool_call_id"]): item for item in results}
        calls = tuple(
            ToolExchangeCall(
                name=str(call["name"]),
                args=dict(call.get("args") or {}),
                result_content=by_id[str(call["id"])].get("content", ""),
                status="error" if by_id[str(call["id"])].get("status") == "error" else "success",
            )
            for call in caller.get("tool_calls") or ()
        )
        refs = (self._ref(source, caller), *(self._ref(source, item) for item in results))
        return ToolExchange(
            type="tool_exchange",
            assistant_content=str(caller.get("content") or ""),
            calls=calls,
            sources=refs,
        )

    @staticmethod
    def _ref(source: ContextRevisionRef, message: dict[str, Any]) -> NamespacedMessageRef:
        message_id = str(message.get("id") or "")
        if not message_id:
            raise ValueError("恢复来源消息缺少稳定 message identity")
        return NamespacedMessageRef(source=source, message_id=message_id)

    @staticmethod
    def _interruption_content(message: dict[str, Any], run_id: str, reason: str) -> str:
        content = str(message.get("content") or "").strip()
        prefix = (
            f"恢复由 Run {run_id} 触发：来源历史有未闭合工具交换，未产生可证明的工具结果；"
            f"工具是否执行或产生副作用未知。触发原因：{reason[:800]}"
        )
        return f"{content}\n\n{prefix}" if content else prefix

    @staticmethod
    def _hash(value: Any) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
