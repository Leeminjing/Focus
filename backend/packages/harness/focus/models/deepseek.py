"""DeepSeek 的 OpenAI 兼容缓存 usage 与 reasoning_content 薄适配。"""

from typing import Any

from langchain_core.messages import AIMessage
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
    """保留 ChatOpenAI 行为，补齐 DeepSeek 缓存与思考字段。"""

    stream_usage: bool | None = True

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ):
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation is not None:
            _attach_cache_read(generation.message, chunk.get("usage"))
            choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
            reasoning = (choices[0].get("delta") or {}).get("reasoning_content") if choices else None
            if reasoning:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return generation

    def _create_chat_result(self, response, generation_info: dict | None = None):
        raw = response if isinstance(response, dict) else response.model_dump()
        result = super()._create_chat_result(response, generation_info)
        for generation, choice in zip(result.generations, raw.get("choices") or []):
            _attach_cache_read(generation.message, raw.get("usage"))
            reasoning = (choice.get("message") or {}).get("reasoning_content")
            if reasoning:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _get_request_payload(
        self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any
    ) -> dict:
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        for message, raw_message in zip(messages, payload.get("messages") or []):
            if isinstance(message, AIMessage) and message.tool_calls:
                reasoning = message.additional_kwargs.get("reasoning_content")
                if reasoning:
                    raw_message["reasoning_content"] = reasoning
        return payload
