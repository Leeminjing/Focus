"""DeepSeek 的 OpenAI 兼容缓存 usage 到 LangChain 标准字段的薄适配。"""

from typing import Any

from langchain_openai import ChatOpenAI


def _attach_cache_read(message: Any, usage: dict[str, Any] | None) -> None:
    if not usage or usage.get("prompt_cache_hit_tokens") is None:
        return
    metadata = getattr(message, "usage_metadata", None)
    if metadata is None:
        return
    details = dict(metadata.get("input_token_details") or {})
    details["cache_read"] = int(usage["prompt_cache_hit_tokens"])
    metadata["input_token_details"] = details


class DeepSeekChatOpenAI(ChatOpenAI):
    """保留 ChatOpenAI 行为，仅补齐 DeepSeek 的缓存命中 token。"""

    stream_usage: bool | None = True

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ):
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation is not None:
            _attach_cache_read(generation.message, chunk.get("usage"))
        return generation

    def _create_chat_result(self, response, generation_info: dict | None = None):
        raw = response if isinstance(response, dict) else response.model_dump()
        result = super()._create_chat_result(response, generation_info)
        for generation in result.generations:
            _attach_cache_read(generation.message, raw.get("usage"))
        return result
