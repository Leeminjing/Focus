r"""本文件对外提供 ModelUsage、StreamUsageAccumulator 与 callback_usage。

输入为 LangChain AIMessage/AIMessageChunk 的 usage_metadata 或 UsageMetadataCallbackHandler 聚合结果；
输出为去重后的模型调用数、输入/输出/cache token。具体工作流为按模型调用 identity 保存累计快照，
同一流式调用只取最新最大值，不把中间 chunk 重复相加；callback 结果则按模型汇总。
示例：`usage = StreamUsageAccumulator(); usage.observe("messages", chunk)`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelUsage:
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: "ModelUsage") -> "ModelUsage":
        return ModelUsage(
            model_calls=self.model_calls + other.model_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )


class StreamUsageAccumulator:
    def __init__(self) -> None:
        self._calls: dict[str, ModelUsage] = {}

    def observe(self, mode: str, chunk: Any) -> None:
        if mode != "messages":
            return
        try:
            message, metadata = chunk
        except (TypeError, ValueError):
            return
        if getattr(message, "type", None) not in {"ai", "AIMessageChunk"}:
            return
        key = self._identity(message, metadata or {})
        usage = self._from_metadata(getattr(message, "usage_metadata", None) or {})
        previous = self._calls.get(key, ModelUsage())
        self._calls[key] = ModelUsage(
            model_calls=1,
            input_tokens=max(previous.input_tokens, usage.input_tokens),
            output_tokens=max(previous.output_tokens, usage.output_tokens),
            cache_read_input_tokens=max(previous.cache_read_input_tokens, usage.cache_read_input_tokens),
        )

    def total(self) -> ModelUsage:
        result = ModelUsage()
        for usage in self._calls.values():
            result += usage
        return result

    @staticmethod
    def _identity(message: Any, metadata: dict[str, Any]) -> str:
        message_id = getattr(message, "id", None)
        if message_id:
            return str(message_id)
        response = getattr(message, "response_metadata", None) or {}
        parts = (
            metadata.get("langgraph_step"),
            metadata.get("langgraph_node"),
            metadata.get("checkpoint_ns"),
            metadata.get("ls_model_name"),
            response.get("model_name"),
        )
        return ":".join(str(part or "") for part in parts)

    @staticmethod
    def _from_metadata(metadata: dict[str, Any]) -> ModelUsage:
        details = metadata.get("input_token_details") or {}
        return ModelUsage(
            input_tokens=_non_negative_int(metadata.get("input_tokens")),
            output_tokens=_non_negative_int(metadata.get("output_tokens")),
            cache_read_input_tokens=_non_negative_int(details.get("cache_read")),
        )


def callback_usage(callback: Any) -> ModelUsage:
    result = ModelUsage()
    for metadata in (getattr(callback, "usage_metadata", None) or {}).values():
        details = metadata.get("input_token_details") or {}
        result += ModelUsage(
            model_calls=1,
            input_tokens=_non_negative_int(metadata.get("input_tokens")),
            output_tokens=_non_negative_int(metadata.get("output_tokens")),
            cache_read_input_tokens=_non_negative_int(details.get("cache_read")),
        )
    return result


def _non_negative_int(value: Any) -> int:
    return int(value) if isinstance(value, int) and value >= 0 else 0
